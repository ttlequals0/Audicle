"""``POST /api/v1/purge`` -- operator-initiated retention sweep.

Requires an explicit ``confirm=true`` query parameter so a stray POST from
a tool or CSRF-less client can't wipe the feed. Accepts ``older_than_days``
to target a subset rather than the full archive.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.services import retention
from app.services.retention import MAX_OLDER_THAN_DAYS

router = APIRouter(tags=["maintenance"])


class PurgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    older_than_days: int = Field(description="Cutoff age in days")
    rows_deleted: int = Field(description="Episode rows removed from the DB")
    files_removed: int = Field(description="MP3/JPG/VTT files removed from disk")
    episode_ids: list[str] = Field(description="IDs of removed episodes")


@router.post(
    "/purge",
    response_model=PurgeResponse,
)
async def post_purge(
    settings: Annotated[Settings, Depends(get_settings)],
    confirm: Annotated[
        bool,
        Query(description="Must be true to acknowledge the destructive action."),
    ] = False,
    older_than_days: Annotated[
        int,
        Query(
            description="Purge episodes older than N days. 0 wipes everything.",
            ge=0,
            le=MAX_OLDER_THAN_DAYS,
        ),
    ] = 0,
) -> PurgeResponse:
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm=true is required to acknowledge the destructive action",
        )
    result = retention.purge_older_than(settings, older_than_days=older_than_days)
    return PurgeResponse(
        older_than_days=older_than_days,
        rows_deleted=result.rows_deleted,
        files_removed=result.files_removed,
        episode_ids=list(result.episode_ids),
    )
