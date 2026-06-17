"""``/api/v1/jobs`` -- admin job inspector + requeue.

``GET /jobs?status=...`` is paginated, with ``X-Total-Count`` for the UI footer;
``status`` filters by ``queued``/``processing``/``done``/``failed`` (omit for all).
``POST /jobs/{id}/requeue`` re-enqueues a terminal job (used by Recents to reprocess
a failed run) -- it works for both URL and uploaded-document jobs.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict

from app.api.deps import get_conn, require_voice_loaded
from app.api.v1.submit import SubmitResponse
from app.config import Settings, get_settings
from app.services import file_extraction, ssrf
from app.services import jobs as jobs_service

router = APIRouter(tags=["jobs"])

_StatusFilter = Literal["queued", "processing", "done", "failed"]


def _source_filename(url: str) -> str | None:
    """The uploaded filename for an ``upload://`` job, else None -- derived from the
    job url (which carries it), so no episodes-table join is needed."""

    if file_extraction.is_upload_source(url):
        return file_extraction.parse_source_uri(url)[1]
    return None


class JobListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    url: str
    episode_id: str
    status: str
    stage: str | None
    error: str | None
    progress_current: int | None
    progress_total: int | None
    created_at: str
    updated_at: str
    started_at: str | None
    # 0.31.0: the uploaded filename (upload jobs only) so Recents can show the source
    # without a broken upload:// link, and whether this run was a reprocess.
    source_filename: str | None
    reprocess: bool


@router.get(
    "/jobs",
    response_model=list[JobListItem],
)
async def list_jobs(
    response: Response,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    status: Annotated[_StatusFilter | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[JobListItem]:
    offset = (page - 1) * per_page
    if status is None:
        total = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        page_rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        ).fetchall()
    else:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status = ?", (status,)
        ).fetchone()["n"]
        page_rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (status, per_page, offset),
        ).fetchall()
    response.headers["X-Total-Count"] = str(total)
    return [
        JobListItem(
            id=row["id"],
            url=row["url"],
            episode_id=row["episode_id"],
            status=row["status"],
            stage=row["stage"],
            error=row["error"],
            progress_current=row["progress_current"],
            progress_total=row["progress_total"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            source_filename=_source_filename(row["url"]),
            reprocess=bool(row["reprocess"]),
        )
        for row in page_rows
    ]


@router.post(
    "/jobs/{job_id}/requeue",
    status_code=201,
    response_model=SubmitResponse,
    summary="Re-enqueue a job (reprocess a failed/finished run)",
    dependencies=[Depends(require_voice_loaded)],
)
async def requeue_job(
    job_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SubmitResponse:
    job = jobs_service.get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    if file_extraction.is_upload_source(job.url):
        # An upload re-runs from the stored original on disk; it's gone once the
        # orphan sweep reaps a never-finalized job's source, so check first.
        _, filename = file_extraction.parse_source_uri(job.url)
        if not file_extraction.source_path(settings, job.episode_id, filename).exists():
            raise HTTPException(
                status_code=409,
                detail="the uploaded file is no longer on disk; re-upload it to reprocess",
            )
    else:
        # URL job: same SSRF guard as /submit before re-fetching.
        try:
            await ssrf.assert_url_public(job.url)
        except ssrf.BlockedHostError as exc:
            if exc.blocked:
                raise HTTPException(
                    status_code=400,
                    detail="The job's URL resolves to a non-public address and was blocked.",
                ) from exc

    try:
        result = jobs_service.create_job(conn, job.url, reprocess=True, voice_id=job.voice_id)
    except jobs_service.DuplicateSubmissionError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "Already queued or processing", "reason": exc.reason},
        ) from exc
    return SubmitResponse(
        job_id=result.job.id,
        episode_id=result.job.episode_id,
        status=result.job.status,
        replaced_previous=result.replaced_previous,
    )
