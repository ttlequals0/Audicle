"""GET /api/v1/status/{job_id} -- look up job state."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.core import database
from app.services import jobs

router = APIRouter(tags=["jobs"])


class StatusResponse(BaseModel):
    job_id: str
    episode_id: str
    url: str
    status: str
    stage: str | None
    error: str | None
    created_at: str
    updated_at: str


@router.get(
    "/status/{job_id}",
    response_model=StatusResponse,
    summary="Fetch job status",
)
def status(
    job_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> StatusResponse:
    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        job = jobs.get_job(conn, job_id)
    finally:
        conn.close()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return StatusResponse(**jobs.job_as_dict(job))
