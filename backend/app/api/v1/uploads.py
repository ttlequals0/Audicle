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
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.api.deps import get_conn, require_voice_loaded
from app.api.v1.submit import SubmitResponse
from app.config import Settings, get_settings
from app.services import episodes as episodes_service
from app.services import file_extraction, jobs, runtime_settings, voices
from app.services.atomic_write import write_bytes_atomic

router = APIRouter(tags=["jobs"])


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


@router.post(
    "/upload",
    status_code=201,
    response_model=SubmitResponse,
    summary="Upload a document (PDF/DOCX/Markdown/text/HTML) for processing",
    dependencies=[Depends(require_voice_loaded)],
)
async def upload(
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
    file: Annotated[UploadFile, File(description="The document to ingest.")],
    reprocess: Annotated[bool, Form()] = False,
    voice: Annotated[str | None, Form()] = None,
) -> SubmitResponse:
    filename = file_extraction.sanitize_filename(file.filename or "")
    ext = file_extraction.extension_of(filename)
    if ext not in file_extraction.ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(file_extraction.ALLOWED_EXTENSIONS))
        raise HTTPException(
            status_code=400,
            detail=f"unsupported file type {ext or '(none)'}; allowed: {allowed}",
        )

    # UPLOAD_MAX_MB is operator-tunable (megabytes); resolve the runtime overlay.
    cap = runtime_settings.overlay(settings).UPLOAD_MAX_MB * 1024 * 1024
    data = await read_upload_capped(file, cap)
    if not data:
        raise HTTPException(status_code=400, detail="uploaded file is empty")

    # Content hash (plus filename) is the identity: the same file re-uploaded maps
    # to the same episode and dedupes like a URL re-submit.
    content_hash = hashlib.sha256(data).hexdigest()
    source_uri = file_extraction.build_source_uri(content_hash, filename)
    episode_id = jobs.compute_episode_id(source_uri)

    # Write the original before enqueueing so the worker can't claim the job before
    # the file exists. A duplicate-without-reprocess 409 below leaves the file in
    # place -- it is byte-identical to the existing episode's stored original.
    write_bytes_atomic(file_extraction.source_path(settings, episode_id, filename), data)

    voice_id = voices.resolve(conn, voice)
    try:
        result = jobs.create_job(conn, source_uri, reprocess=reprocess, voice_id=voice_id)
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
    return SubmitResponse(
        job_id=result.job.id,
        episode_id=result.job.episode_id,
        status=result.job.status,
        replaced_previous=result.replaced_previous,
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
    episode = episodes_service.get_by_id(conn, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    if episode.source_type != "upload":
        raise HTTPException(
            status_code=400,
            detail="episode was not created from an uploaded file; reprocess it via its URL",
        )
    stored = file_extraction.source_path(settings, episode_id, episode.source_filename or "")
    if not stored.exists():
        raise HTTPException(
            status_code=409,
            detail="the stored original is no longer on disk; re-upload the document to reprocess",
        )
    try:
        result = jobs.create_job(conn, episode.original_url, reprocess=True)
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
