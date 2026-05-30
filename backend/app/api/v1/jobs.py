"""``GET /api/v1/jobs?status=...`` -- admin job inspector.

Paginated, ``X-Total-Count`` for the UI footer. ``status`` query param
filters by ``queued``/``processing``/``done``/``failed``; omit to list all.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict

from app.config import Settings, get_settings
from app.core import database

router = APIRouter(tags=["jobs"])

_StatusFilter = Literal["queued", "processing", "done", "failed"]


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


@router.get(
    "/jobs",
    response_model=list[JobListItem],
)
async def list_jobs(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    status: Annotated[_StatusFilter | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[JobListItem]:
    offset = (page - 1) * per_page
    with database.connection(settings.DATA_DIR) as conn:
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
        )
        for row in page_rows
    ]
