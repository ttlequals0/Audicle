"""``GET /media/{episode_id}.{mp3,jpg,vtt}`` handlers.

mp3 and jpg are served from disk via ``FileResponse``; vtt is rendered from
the episode row's ``transcript_vtt`` column (no separate file on disk so
operators don't have to back up two copies of the transcript).

The episode_id pattern is constrained to the alphabet jobs.py generates so
no caller can use ``..`` or absolute paths to escape the media directory.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse

from app.config import Settings, get_settings
from app.core import database
from app.core.paths import media_dir
from app.services import episodes

router = APIRouter(prefix="/media", tags=["media"])

# jobs.py generates episode_ids as short hex tokens. This pattern enforces
# the contract at the route boundary so a malformed id 404s before any
# filesystem lookup -- defence-in-depth against path traversal.
_EPISODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_episode_id(episode_id: str) -> None:
    if not _EPISODE_ID_RE.match(episode_id):
        raise HTTPException(status_code=404, detail="not found")


def _safe_path(root: Path, name: str) -> Path:
    """Resolve and confirm the result still lives under ``root``."""

    candidate = (root / name).resolve()
    root_resolved = root.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    return candidate


@router.get("/{episode_id}.mp3")
async def get_mp3(
    episode_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    _validate_episode_id(episode_id)
    path = _safe_path(media_dir(settings), f"{episode_id}.mp3")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(
        path,
        media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/{episode_id}.jpg")
async def get_jpg(
    episode_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    _validate_episode_id(episode_id)
    path = _safe_path(media_dir(settings), f"{episode_id}.jpg")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/{episode_id}.vtt")
async def get_vtt(
    episode_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    _validate_episode_id(episode_id)
    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        episode = episodes.get_by_id(conn, episode_id)
    finally:
        conn.close()
    if episode is None or not episode.transcript_vtt:
        raise HTTPException(status_code=404, detail="not found")
    return Response(
        content=episode.transcript_vtt,
        media_type="text/vtt; charset=utf-8",
        headers={"Cache-Control": "public, max-age=86400"},
    )
