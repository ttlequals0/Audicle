"""``GET /rss/rss.xml`` -- the Podcasting 2.0 RSS feed.

Channel metadata comes from ``Settings``; episodes come from the
``episodes`` table; ``podcast:guid`` is initialized once and persisted via
``services.settings_store``.

HTTP caching: ``Last-Modified`` is set from the newest episode's
``updated_at`` (or the channel build time if there are no episodes), and
``If-Modified-Since`` round-trips to a ``304 Not Modified`` so podcast
clients don't refetch the full body on every poll.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response

from app.config import Settings, get_settings
from app.core import database
from app.services import episodes, feed, runtime_settings, settings_store

router = APIRouter(prefix="/rss", tags=["rss"])


@router.get("/rss.xml")
async def get_rss(
    base_settings: Annotated[Settings, Depends(get_settings)],
    if_modified_since: Annotated[str | None, Header()] = None,
) -> Response:
    # Apply the runtime_settings overlay so an operator PUT to
    # /api/v1/settings (FEED_TITLE, FEED_DESCRIPTION, FEED_LANGUAGE, etc.)
    # is reflected on the next RSS render without a process restart.
    settings = runtime_settings.overlay(base_settings)
    with database.connection(settings.DATA_DIR) as conn:
        rows = episodes.list_published(conn)
        latest = episodes.latest_updated_at(conn)
        guid = settings_store.get_or_init_podcast_guid(conn, settings.BASE_URL)

    last_build = _last_build_datetime(latest)
    not_modified = _is_not_modified(if_modified_since, last_build)
    headers = {
        "Last-Modified": format_datetime(last_build, usegmt=True),
        "Cache-Control": f"public, max-age={settings.RSS_CACHE_MAX_AGE_SECONDS}",
    }
    if not_modified:
        return Response(status_code=304, headers=headers)

    body = feed.render(
        rows,
        settings=settings,
        podcast_guid=guid,
        last_build=last_build,
    )
    return Response(
        content=body,
        media_type="application/rss+xml; charset=utf-8",
        headers=headers,
    )


def _last_build_datetime(latest: str | None) -> datetime:
    if latest is None:
        return datetime.now(UTC).replace(microsecond=0)
    parsed = feed._parse_iso(latest)
    return parsed.astimezone(UTC) if parsed else datetime.now(UTC).replace(microsecond=0)


def _is_not_modified(if_modified_since: str | None, last_build: datetime) -> bool:
    if not if_modified_since:
        return False
    try:
        client_time = parsedate_to_datetime(if_modified_since)
    except (TypeError, ValueError):
        return False
    if client_time.tzinfo is None:
        client_time = client_time.replace(tzinfo=UTC)
    # HTTP-date precision is one second; >= comparison so a client with the
    # exact build time gets 304 rather than a redundant full body.
    return client_time >= last_build.replace(microsecond=0)
