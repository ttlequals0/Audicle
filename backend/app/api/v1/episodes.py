"""``/api/v1/episodes`` -- admin UI list, delete, and chapter regeneration.

The list endpoint paginates via ``page`` + ``per_page`` query params, filters on
``q`` (title, source URL, or uploaded filename), and returns ``X-Total-Count``
for the filtered set so the UI can render a pager. Delete removes
the DB row + on-disk media via the existing retention helpers.
``POST /episodes/{id}/chapters`` rebuilds chapters from the stored transcript.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_conn
from app.config import Settings, get_settings
from app.core.paths import media_dir
from app.services import episodes as episodes_service
from app.services import pipeline, runtime_settings, transcript
from app.services.retention import _remove_path

logger = logging.getLogger("app.api.episodes")

# Episode ids are hex; the id lands in a filesystem path below.
_EPISODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

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
    # True once chapters exist (0.51.0+), so the UI only offers the link when
    # there is a document to open.
    has_chapters: bool
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
    per_page: Annotated[int, Query(ge=1, le=500)] = 25,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> list[EpisodeListItem]:
    total = episodes_service.count_published(conn, q=q)
    page_rows = episodes_service.list_published_page(
        conn, limit=per_page, offset=(page - 1) * per_page, q=q
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
            has_chapters=bool(ep.chapters_json),
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


class ChaptersRegenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_id: str
    chapter_count: int


@router.post(
    "/episodes/{episode_id}/chapters",
    response_model=ChaptersRegenResponse,
)
async def regenerate_chapters(
    episode_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> ChaptersRegenResponse:
    """Rebuild chapters for a finished episode from its stored transcript.

    Cues are one per TTS chunk, so the transcript already carries the timeline
    the chapter stage used; no TTS or re-encode is needed. Existing chapters
    survive a failed run, and the episode GUID is left alone because the audio
    does not change."""

    if not _EPISODE_ID_RE.match(episode_id):
        raise HTTPException(status_code=404, detail="episode not found")
    episode = episodes_service.get_by_id(conn, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    if not episode.transcript_vtt:
        raise HTTPException(
            status_code=409,
            detail="episode has no transcript to derive chapter timings from",
        )
    # Effective settings, not env-only: the LLM connection and the chapter
    # tunables normally live in runtime_settings, the same overlay the worker
    # applies per job.
    settings = runtime_settings.overlay(settings)
    cues = transcript.cues_from_vtt(episode.transcript_vtt)
    if not cues:
        raise HTTPException(status_code=409, detail="transcript has no cues")
    duration = float(episode.duration_secs or 0) or (cues[-1][0] + 1)
    # Resolve the MP3 the same way the media route serves it, rather than
    # trusting the stored path. None when retention already pruned the audio;
    # the feed JSON still updates.
    mp3_path: Path | None = media_dir(settings) / f"{episode_id}.mp3"
    if not mp3_path.is_file():
        mp3_path = None
    document = await pipeline.generate_chapters_document(cues, duration, mp3_path, settings)
    if document is None:
        raise HTTPException(
            status_code=422,
            detail="chapter generation produced no chapters; existing chapters kept",
        )
    episodes_service.set_chapters(conn, episode_id, document)
    count = len(json.loads(document)["chapters"])
    logger.info(
        "Chapters regenerated",
        extra={
            "event": "chapters_regenerated",
            "episode_id": episode_id,
            "chapter_count": count,
        },
    )
    return ChaptersRegenResponse(episode_id=episode_id, chapter_count=count)


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
