"""``GET /api/v1/episodes`` and ``DELETE /api/v1/episodes/{id}`` -- admin UI.

The list endpoint paginates via ``page`` + ``per_page`` query params and
returns ``X-Total-Count`` so the UI can render a footer. Delete removes
the DB row + on-disk media via the existing retention helpers.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_conn
from app.config import Settings, get_settings
from app.core.paths import media_dir
from app.services import episodes as episodes_service
from app.services.retention import _remove_path

router = APIRouter(tags=["episodes"])


class EpisodeListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str | None
    author: str | None
    original_url: str
    audio_path: str | None
    audio_size_bytes: int | None
    artwork_path: str | None
    duration_secs: int | None
    pub_date: str
    updated_at: str
    # True once the cleaned article text exists (0.6.0+); the UI gates the
    # /media/{id}.txt download link on it so older episodes show no dead link.
    has_cleaned_text: bool
    # Source provenance (0.30.0): 'url' or 'upload'. The UI renders an upload's
    # filename instead of a hyperlink and routes its reprocess to /upload/{id}/reprocess.
    source_type: str
    source_filename: str | None
    # Which reference voice narrated the episode (0.31.x): a slot label, "Slot N",
    # or "Default". NULL only for old rows finalized before the column existed.
    voice_label: str | None


@router.get(
    "/episodes",
    response_model=list[EpisodeListItem],
)
async def list_episodes(
    response: Response,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[EpisodeListItem]:
    total = episodes_service.count_published(conn)
    page_rows = episodes_service.list_published_page(
        conn, limit=per_page, offset=(page - 1) * per_page
    )
    with_text = episodes_service.ids_with_cleaned_text(
        conn, [ep.id for ep in page_rows]
    )
    response.headers["X-Total-Count"] = str(total)
    return [
        EpisodeListItem(
            id=ep.id,
            title=ep.title,
            author=ep.author,
            original_url=ep.original_url,
            audio_path=ep.audio_path,
            audio_size_bytes=episodes_service.audio_size(ep),
            artwork_path=ep.artwork_path,
            duration_secs=ep.duration_secs,
            pub_date=ep.pub_date,
            updated_at=ep.updated_at,
            has_cleaned_text=ep.id in with_text,
            source_type=ep.source_type,
            source_filename=ep.source_filename,
            voice_label=ep.voice_label,
        )
        for ep in page_rows
    ]


class DeleteEpisodeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    files_removed: int = Field(ge=0)


@router.delete(
    "/episodes/{episode_id}",
    response_model=DeleteEpisodeResponse,
)
async def delete_episode(
    episode_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DeleteEpisodeResponse:
    episode = episodes_service.get_by_id(conn, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
    conn.commit()
    out_root = media_dir(settings)
    from pathlib import Path

    files_removed = 0
    for path_str in (episode.audio_path, episode.artwork_path):
        if path_str and _remove_path(Path(path_str), root_guard=out_root):
            files_removed += 1
    if _remove_path(out_root / f"{episode_id}.vtt", root_guard=out_root):
        files_removed += 1
    # An uploaded episode also has its stored original ({id}.source.{ext}).
    for src in out_root.glob(f"{episode_id}.source.*"):
        if _remove_path(src, root_guard=out_root):
            files_removed += 1
    return DeleteEpisodeResponse(id=episode_id, files_removed=files_removed)
