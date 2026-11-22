"""``POST /api/v1/upload`` -- ingest a directly-uploaded document.

The URL path (``/submit``) fetches an article via Firecrawl; this path accepts a
file the operator already has (PDF/DOCX/Markdown/text/HTML), stores the original
on disk, and enqueues a normal job whose synthetic ``upload://`` source the
worker reads back. From the cleanup stage onward the pipeline is identical, so an
uploaded document becomes an episode exactly like a URL.

``/upload/{episode_id}/reprocess`` re-runs an existing upload episode from its
stored original with no re-upload -- the reason the original is kept on disk.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile

from app.api.deps import get_conn, require_voice_loaded
from app.api.v1.books import BookResponse, book_response, queue_selection
from app.api.v1.submit import SubmitResponse
from app.config import Settings, get_settings
from app.core import database
from app.services import book_upload, books, file_extraction, jobs, runtime_settings, voices
from app.services import episodes as episodes_service
from app.services.atomic_write import write_bytes_atomic

router = APIRouter(tags=["jobs"])
_EPISODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SPLIT_EXTENSIONS = (".epub", ".pdf")


async def read_upload_capped(upload: UploadFile, cap: int) -> bytes:
    """Stream the upload into memory, aborting with 400 once it crosses ``cap``
    so a hostile payload can't fully buffer first."""

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise HTTPException(status_code=400, detail=f"file must be <= {cap} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _queue_chapters(
    conn: sqlite3.Connection, settings: Settings, book_id: str, chapters: str, voice: str | None
) -> BookResponse:
    with database.upload_staging_lock(settings.DATA_DIR, book_id):
        book = books.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=409, detail="the book changed during upload; retry")
        try:
            return queue_selection(conn, book, chapters, voice)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/upload",
    status_code=201,
    response_model=SubmitResponse | BookResponse,
    summary="Upload a document (PDF/DOCX/Markdown/text/HTML) for processing",
    dependencies=[Depends(require_voice_loaded)],
)
async def upload(
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
    response: Response,
    file: Annotated[UploadFile, File(description="The document to ingest.")],
    reprocess: Annotated[bool, Form()] = False,
    voice: Annotated[str | None, Form()] = None,
    split_chapters: Annotated[
        bool, Form(description="Split an EPUB or PDF into one episode per chapter.")
    ] = False,
    chapters: Annotated[
        str | None,
        Form(description="With split_chapters: all, default, or a list such as 1,3,5-9."),
    ] = None,
) -> SubmitResponse | BookResponse:
    if chapters is not None and not split_chapters:
        raise HTTPException(status_code=400, detail="chapters requires split_chapters")
    filename = file_extraction.sanitize_filename(file.filename or "")
    ext = file_extraction.extension_of(filename)
    if ext not in file_extraction.ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(file_extraction.ALLOWED_EXTENSIONS))
        raise HTTPException(
            status_code=400,
            detail=f"unsupported file type {ext or '(none)'}; allowed: {allowed}",
        )

    if split_chapters and ext not in _SPLIT_EXTENSIONS:
        raise HTTPException(status_code=400, detail="split_chapters supports only EPUB and PDF")

    # UPLOAD_MAX_MB is operator-tunable (megabytes); resolve the runtime overlay.
    effective = runtime_settings.overlay(settings)
    cap = effective.UPLOAD_MAX_MB * 1024 * 1024
    data = await read_upload_capped(file, cap)
    if not data:
        raise HTTPException(status_code=400, detail="uploaded file is empty")

    # Content hash (plus filename) is the identity: the same file re-uploaded maps
    # to the same episode and dedupes like a URL re-submit.
    content_hash = hashlib.sha256(data).hexdigest()
    source_uri = file_extraction.build_source_uri(content_hash, filename)
    episode_id = jobs.compute_episode_id(source_uri)

    if split_chapters:
        try:
            found = await book_upload.ensure_book(
                conn,
                settings,
                data=data,
                ext=ext,
                filename=filename,
                source_uri=source_uri,
                chapters=chapters,
                min_chars=effective.MIN_EXTRACTION_CHARS,
            )
        except ValueError as exc:  # BookFormatError or a bad `chapters` selection
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except book_upload.BookChangedError as exc:
            raise HTTPException(
                status_code=409, detail="the book changed during upload; retry"
            ) from exc
        if found is not None:
            book, created = found
            response.status_code = 201 if created else 200
            if chapters is None:
                return book_response(conn, book)
            return _queue_chapters(conn, settings, book.id, chapters, voice)
        # No chapters found: the `chapters` value is ignored and the file is one episode.

    with database.upload_staging_lock(settings.DATA_DIR, episode_id):
        voice_id = voices.resolve(conn, voice)
        try:
            result = jobs.create_job(
                conn, source_uri, reprocess=reprocess, voice_id=voice_id, staging=True
            )
        except jobs.DuplicateSubmissionError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "Episode already exists",
                    "details": {
                        "episode_id": exc.episode_id,
                        "reason": exc.reason,
                        "filename": filename,
                    },
                },
            ) from exc
        try:
            write_bytes_atomic(file_extraction.source_path(settings, episode_id, filename), data)
            job = jobs.queue_staged(conn, result.job.id)
        except Exception:
            jobs.mark_cancelled(conn, result.job.id)
            raise
    return SubmitResponse(
        job_id=job.id,
        episode_id=job.episode_id,
        status=job.status,
        replaced_previous=result.replaced_previous,
        chapters_detected=False if split_chapters else None,
    )


@router.post(
    "/upload/{episode_id}/reprocess",
    status_code=201,
    response_model=SubmitResponse,
    summary="Reprocess an uploaded episode from its stored original",
    dependencies=[Depends(require_voice_loaded)],
)
async def reprocess_upload(
    episode_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SubmitResponse:
    if not _EPISODE_ID_RE.fullmatch(episode_id):
        raise HTTPException(status_code=404, detail="episode not found")
    episode = episodes_service.get_by_id(conn, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    if episode.source_type != "upload":
        raise HTTPException(
            status_code=400,
            detail="episode was not created from an uploaded file; reprocess it via its URL",
        )
    chapter = books.chapter_for_episode(conn, episode_id)
    try:
        result = file_extraction.requeue_stored(
            conn,
            settings,
            episode.original_url,
            episode_id,
            chapter[0].voice_id if chapter else None,
        )
    except file_extraction.StoredSourceMissingError as exc:
        raise HTTPException(
            status_code=409,
            detail="the stored original is no longer on disk; re-upload the document to reprocess",
        ) from exc
    except jobs.DuplicateSubmissionError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "Reprocess already in flight",
                "details": {"episode_id": exc.episode_id, "reason": exc.reason},
            },
        ) from exc
    return SubmitResponse(
        job_id=result.job.id,
        episode_id=result.job.episode_id,
        status=result.job.status,
        replaced_previous=result.replaced_previous,
    )
