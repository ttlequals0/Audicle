"""``POST /api/v1/feed/recreate`` -- rotate the feed + episode GUIDs.

API-only (deliberately not surfaced in the UI). Requires an explicit
``confirm=true`` query parameter, mirroring ``/purge``: rotating the channel
``podcast:guid`` signals aggregators this is a new feed, and salting every
episode ``<guid>`` makes podcast apps re-download all episodes. Intended for
troubleshooting clients stuck on a stale, long-cached feed; disruptive to
existing subscribers, hence the confirm gate.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import require_admin
from app.config import Settings, get_settings
from app.core import database
from app.services import settings_store

router = APIRouter(tags=["maintenance"])


class FeedRecreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    podcast_guid: str = Field(description="The new channel-level podcast:guid")
    guid_epoch: int = Field(description="Counter now appended to every episode <guid>")


@router.post(
    "/feed/recreate",
    response_model=FeedRecreateResponse,
    dependencies=[Depends(require_admin)],
)
async def post_feed_recreate(
    settings: Annotated[Settings, Depends(get_settings)],
    confirm: Annotated[
        bool,
        Query(description="Must be true to acknowledge the disruptive action."),
    ] = False,
) -> FeedRecreateResponse:
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm=true is required to acknowledge the disruptive action",
        )
    with database.connection(settings.DATA_DIR) as conn:
        guid, epoch = settings_store.rotate_feed_guids(conn, settings.BASE_URL)
    return FeedRecreateResponse(podcast_guid=guid, guid_epoch=epoch)
