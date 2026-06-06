"""``GET /rss/{slug}.xml`` -- the Podcasting 2.0 RSS feed.

The path slug is derived from ``FEED_TITLE`` (e.g. "Articles of Interest" ->
``/rss/articles_of_interest.xml``); a request for any other slug 404s, so the
feed URL tracks the feed name and a rename effectively retires the old URL.

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

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from app.config import Settings, get_settings
from app.core import database
from app.services import episodes, feed, runtime_settings, settings_store
from app.services import slug as slug_module

router = APIRouter(prefix="/rss", tags=["rss"])


# GET + HEAD: Apple Podcasts and other platforms issue a HEAD before GET and
# treat a 405 as a hard failure, so the feed must answer HEAD with the same
# headers and an empty body.
@router.api_route("/{slug}.xml", methods=["GET", "HEAD"])
async def get_rss(
    slug: str,
    request: Request,
    base_settings: Annotated[Settings, Depends(get_settings)],
    if_modified_since: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    # Apply the runtime_settings overlay so an operator PUT to
    # /api/v1/settings (FEED_TITLE, FEED_DESCRIPTION, FEED_LANGUAGE, etc.)
    # is reflected on the next RSS render without a process restart.
    settings = runtime_settings.overlay(base_settings)
    # The feed lives at exactly one slug -- the current FEED_TITLE's. Any other
    # slug (the old /rss/rss.xml, or a pre-rename name) is a different feed and
    # 404s, per the "rename = new feed" contract.
    if slug != slug_module.feed_slug(settings.FEED_TITLE):
        raise HTTPException(status_code=404, detail="not found")
    with database.connection(settings.DATA_DIR) as conn:
        rows = episodes.list_published(conn)
        latest = episodes.latest_updated_at(conn)
        guid = settings_store.get_or_init_podcast_guid(conn, settings.BASE_URL)
        guid_epoch = settings_store.get_feed_guid_epoch(conn)

    last_build = _last_build_datetime(latest)
    etag = _feed_etag(last_build, guid_epoch, len(rows))
    media_type = "application/rss+xml; charset=utf-8"
    headers = {
        "Last-Modified": format_datetime(last_build, usegmt=True),
        "Cache-Control": f"public, max-age={settings.RSS_CACHE_MAX_AGE_SECONDS}",
        "ETag": etag,
    }
    if _is_not_modified(if_modified_since, last_build) or _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    # HEAD: headers only, and skip the (gzip-able) render entirely.
    if request.method == "HEAD":
        return Response(status_code=200, headers=headers, media_type=media_type)

    body = feed.render(
        rows,
        settings=settings,
        podcast_guid=guid,
        last_build=last_build,
        feed_guid_epoch=guid_epoch,
    )
    return Response(content=body, media_type=media_type, headers=headers)


def _feed_etag(last_build: datetime, guid_epoch: int, episode_count: int) -> str:
    """Weak validator: changes when an episode updates (last_build), the feed is
    recreated (guid_epoch), or the episode count changes -- the same inputs that
    drive Last-Modified, so it never goes stale relative to it."""

    return f'W/"{int(last_build.timestamp())}-{guid_epoch}-{episode_count}"'


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    candidates = {token.strip() for token in if_none_match.split(",")}
    return etag in candidates or "*" in candidates


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
