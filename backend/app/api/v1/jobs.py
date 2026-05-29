"""``GET /api/v1/jobs?status=...`` -- admin job inspector.

Paginated, ``X-Total-Count`` for the UI footer. ``status`` query param
filters by ``queued``/``processing``/``done``/``failed``; omit to list all.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict

from app.api.deps import require_admin
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
    created_at: str
    updated_at: str


@router.get(
    "/jobs",
    response_model=list[JobListItem],
    dependencies=[Depends(require_admin)],
)
async def list_jobs(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    status: Annotated[_StatusFilter | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[JobListItem]:
    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        if status is None:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
    finally:
        conn.close()
    total = len(rows)
    start = (page - 1) * per_page
    page_rows = rows[start : start + per_page]
    response.headers["X-Total-Count"] = str(total)
    return [
        JobListItem(
            id=row["id"],
            url=row["url"],
            episode_id=row["episode_id"],
            status=row["status"],
            stage=row["stage"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        for row in page_rows
    ]
