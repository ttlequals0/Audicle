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

import asyncio
import hashlib
import io
import re
import shutil
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from PIL import Image, UnidentifiedImageError

from app.api.deps import get_conn, require_voice_loaded
from app.api.v1.books import BookResponse, book_response, queue_selection
from app.api.v1.submit import SubmitResponse
from app.config import Settings, get_settings
from app.core import database
from app.core.paths import book_dir
from app.services import book_chapters, books, file_extraction, jobs, runtime_settings, voices
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


def _prepare_book(
    data: bytes, ext: str, min_chars: int
) -> tuple[book_chapters.DetectedBook, bytes | None] | None:
    """CPU-bound: detect chapters and re-encode the cover as JPEG (None when unreadable)."""

    detected = book_chapters.detect(data, ext, min_chars=min_chars)
    if detected is None:
        return None
    cover: bytes | None = None
    if detected.cover:
        try:
            image = Image.open(io.BytesIO(detected.cover))
            limit = Image.MAX_IMAGE_PIXELS
            if not limit or image.width * image.height <= limit:
                out = io.BytesIO()
                image.convert("RGB").save(out, format="JPEG")
                cover = out.getvalue()
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
            cover = None
    return detected, cover


def _create_book(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    data: bytes,
    ext: str,
    source_uri: str,
    filename: str,
    detected: book_chapters.DetectedBook,
    cover: bytes | None,
) -> books.Book:
    book_id = books.book_id_for(source_uri)
    try:
        write_bytes_atomic(books.source_file(settings, book_id, ext), data)
        if cover is not None:
            write_bytes_atomic(books.cover_file(settings, book_id), cover)
        return books.create_book(
            conn,
            source_uri=source_uri,
            filename=filename,
            detected=detected,
            has_cover=cover is not None,
        )
    except Exception:
        shutil.rmtree(book_dir(settings, book_id), ignore_errors=True)
        raise


async def _split_upload(
    conn: sqlite3.Connection,
    settings: Settings,
    response: Response,
    *,
    data: bytes,
    ext: str,
    filename: str,
    source_uri: str,
    chapters: str | None,
    voice: str | None,
    min_chars: int,
) -> BookResponse | None:
    """Create or fetch the book and queue ``chapters``. None means no chapters were found.

    Detection runs off the event loop and never while the flock is held (a blocking
    flock across an await would stall the loop for a second request on the same file).
    """

    book_id = books.book_id_for(source_uri)
    prepared = None
    for _attempt in range(2):
        if prepared is None and books.get_book(conn, book_id) is None:
            try:
                prepared = await asyncio.to_thread(_prepare_book, data, ext, min_chars)
            except book_chapters.BookFormatError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if prepared is None:
                return None
            try:
                if chapters is not None:
                    books.validate_selection(chapters, prepared[0].chapters)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        with database.upload_staging_lock(settings.DATA_DIR, book_id):
            book = books.get_book(conn, book_id)
            if book is None and prepared is None:
                continue  # deleted since the pre-check: prepare, then retry under the lock
            created = book is None
            if book is None:
                detected, cover = prepared
                book = _create_book(
                    conn,
                    settings,
                    data=data,
                    ext=ext,
                    source_uri=source_uri,
                    filename=filename,
                    detected=detected,
                    cover=cover,
                )
            response.status_code = 201 if created else 200
            if chapters is None:
                return book_response(conn, book)
            try:
                return queue_selection(conn, book, chapters, voice)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=409, detail="the book changed during upload; retry")


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

    chapters_detected = None
    if split_chapters:
        book_result = await _split_upload(
            conn,
            settings,
            response,
            data=data,
            ext=ext,
            filename=filename,
            source_uri=source_uri,
            chapters=chapters,
            voice=voice,
            min_chars=effective.MIN_EXTRACTION_CHARS,
        )
        if book_result is not None:
            return book_result
        # No chapters found: the `chapters` value is ignored and the file is one episode.
        chapters_detected = False

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
        chapters_detected=chapters_detected,
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
    with database.upload_staging_lock(settings.DATA_DIR, episode_id):
        episode = episodes_service.get_by_id(conn, episode_id)
        if episode is None:
            raise HTTPException(status_code=404, detail="episode not found")
        if episode.source_type != "upload":
            raise HTTPException(
                status_code=400,
                detail="episode was not created from an uploaded file; reprocess it via its URL",
            )
        stored = file_extraction.stored_source_path(settings, episode.original_url, episode_id)
        if not stored.exists():
            raise HTTPException(
                status_code=409,
                detail="the stored original is no longer on disk; re-upload the document to reprocess",
            )
        chapter = books.chapter_for_episode(conn, episode_id)
        voice_id = chapter[0].voice_id if chapter else None
        try:
            result = jobs.create_job(conn, episode.original_url, reprocess=True, voice_id=voice_id)
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
