"""``GET /media/{episode_id}.{mp3,jpg,vtt}`` handlers.

mp3 and jpg are served from disk via ``FileResponse``; vtt is rendered from
the episode row's ``transcript_vtt`` column (no separate file on disk so
operators don't have to back up two copies of the transcript).

The episode_id pattern is constrained to the alphabet jobs.py generates so
no caller can use ``..`` or absolute paths to escape the media directory.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse

from app.api.deps import get_conn, require_feed_key
from app.config import Settings, get_settings
from app.core.paths import media_dir
from app.services import episodes, feed_auth

# require_feed_key gates every asset when FEED_AUTH_ENABLED (no-op otherwise).
router = APIRouter(prefix="/media", tags=["media"], dependencies=[Depends(require_feed_key)])

# jobs.py generates episode_ids as short hex tokens. This pattern enforces
# the contract at the route boundary so a malformed id 404s before any
# filesystem lookup -- defence-in-depth against path traversal.
# NOTE: the default podcast artwork is served as ``/media/default.jpg`` (seeded
# to DATA_DIR/media on startup); "default" must remain matchable by this pattern.
_EPISODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_episode_id(episode_id: str) -> None:
    if not _EPISODE_ID_RE.match(episode_id):
        raise HTTPException(status_code=404, detail="not found")


def _stored_media_path(settings: Settings, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=404, detail="not found")
    path = Path(value).resolve()
    try:
        path.relative_to(media_dir(settings).resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return path


@router.get("/{episode_id}.mp3", operation_id="get_media_mp3")
@router.head("/{episode_id}.mp3", operation_id="head_media_mp3")
async def get_mp3(
    episode_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    v: Annotated[str | None, Query()] = None,
) -> FileResponse:
    _validate_episode_id(episode_id)
    audio_path = episodes.generation_value(conn, episode_id, v, "audio_path")
    path = _stored_media_path(settings, audio_path)
    return FileResponse(
        path,
        media_type="audio/mpeg",
        headers={"Cache-Control": feed_auth.cache_control(conn, settings, 86400)},
    )


@router.get("/{episode_id}.jpg", operation_id="get_media_jpg")
@router.head("/{episode_id}.jpg", operation_id="head_media_jpg")
async def get_jpg(
    episode_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    v: Annotated[str | None, Query()] = None,
) -> FileResponse:
    # A keyed cover URL is /media/<episode_id>-<key>.jpg; require_feed_key has
    # already validated the key, so take the id half for the file lookup.
    episode_id, _ = feed_auth.split_cover_token(episode_id)
    episode_id, generation_token = feed_auth.split_cover_generation(episode_id)
    _validate_episode_id(episode_id)
    artwork_path = episodes.generation_value(
        conn, episode_id, generation_token or v, "artwork_path"
    )
    path = _stored_media_path(settings, artwork_path)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": feed_auth.cache_control(conn, settings, 86400)},
    )


@router.get("/{episode_id}.vtt", operation_id="get_media_vtt")
@router.head("/{episode_id}.vtt", operation_id="head_media_vtt")
async def get_vtt(
    episode_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
    v: Annotated[str | None, Query()] = None,
) -> Response:
    _validate_episode_id(episode_id)
    transcript_vtt = episodes.generation_value(conn, episode_id, v, "transcript_vtt")
    if not isinstance(transcript_vtt, str) or not transcript_vtt:
        raise HTTPException(status_code=404, detail="not found")
    return Response(
        content=transcript_vtt,
        media_type="text/vtt; charset=utf-8",
        headers={"Cache-Control": feed_auth.cache_control(conn, settings, 86400)},
    )


@router.get("/{episode_id}.chapters.json", operation_id="get_media_chapters")
@router.head("/{episode_id}.chapters.json", operation_id="head_media_chapters")
async def get_chapters(
    episode_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
    v: Annotated[str | None, Query()] = None,
) -> Response:
    """The Podcasting 2.0 chapters document, served from the ``chapters_json``
    column. 404 for episodes without chapters (short, disabled, or pre-0.51.0)."""

    _validate_episode_id(episode_id)
    chapters_json = episodes.generation_value(conn, episode_id, v, "chapters_json")
    if not isinstance(chapters_json, str) or not chapters_json:
        raise HTTPException(status_code=404, detail="not found")
    return Response(
        content=chapters_json,
        media_type="application/json+chapters",
        headers={"Cache-Control": feed_auth.cache_control(conn, settings, 86400)},
    )


@router.get("/{episode_id}.txt", operation_id="get_media_text")
@router.head("/{episode_id}.txt", operation_id="head_media_text")
async def get_cleaned_text(
    episode_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
    v: Annotated[str | None, Query()] = None,
) -> Response:
    """The cleaned article text (the exact input to TTS), served from the
    ``cleaned_text`` column. 404 for episodes processed before 0.6.0 (NULL)."""

    _validate_episode_id(episode_id)
    cleaned_text = episodes.generation_value(conn, episode_id, v, "cleaned_text")
    if not isinstance(cleaned_text, str) or not cleaned_text:
        raise HTTPException(status_code=404, detail="not found")
    return Response(
        content=cleaned_text,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": feed_auth.cache_control(conn, settings, 86400)},
    )
