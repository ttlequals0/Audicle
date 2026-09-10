"""``GET /rss/{slug}.xml`` -- the Podcasting 2.0 RSS feed.

The path slug is derived from ``FEED_TITLE`` (e.g. "Articles of Interest" ->
``/rss/articles_of_interest.xml``); a request for any other slug 404s, so the
feed URL tracks the feed name and a rename effectively retires the old URL.

Channel metadata comes from ``Settings``; episodes come from the
``episodes`` table; ``podcast:guid`` is initialized once and persisted via
``services.settings_store``.

HTTP validators come from a persistent feed representation revision. HEAD and
unchanged conditional requests resolve that small state before loading episode
rows, and rendered bytes are cached by the same complete identity.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import OrderedDict
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from app.api.deps import get_conn, require_feed_key
from app.config import Settings, get_settings
from app.services import episodes, feed, feed_auth, feed_revision, runtime_settings, settings_store
from app.services import slug as slug_module

# require_feed_key gates the whole feed when FEED_AUTH_ENABLED (no-op otherwise).
router = APIRouter(prefix="/rss", tags=["rss"], dependencies=[Depends(require_feed_key)])
_rendered_feeds: OrderedDict[tuple[str, int, str, str], bytes] = OrderedDict()
_RENDERED_FEED_CACHE_SIZE = 8
_RENDERED_FEED_CACHE_BYTES = 16 * 1024 * 1024


# GET + HEAD: Apple Podcasts and other platforms issue a HEAD before GET and
# treat a 405 as a hard failure, so the feed must answer HEAD with the same
# headers and an empty body.
@router.get("/{slug}.xml", operation_id="get_rss_feed")
@router.head("/{slug}.xml", operation_id="head_rss_feed")
async def get_rss(
    slug: str,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    if_modified_since: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    # Resolve settings and feed identity from one database snapshot so a
    # concurrent settings save cannot pair new metadata with an old validator.
    # The feed lives at exactly one slug -- the current FEED_TITLE's. Any other
    # slug (the old /rss/rss.xml, or a pre-rename name) is a different feed and
    # 404s, per the "rename = new feed" contract.
    settings_store.get_or_init_podcast_guid(conn, settings.BASE_URL)
    settings, identity = _feed_snapshot(conn, settings)
    require_feed_key(request, conn, settings)
    if slug != slug_module.feed_slug(settings.FEED_TITLE):
        raise HTTPException(status_code=404, detail="not found")
    guid = settings_store.get(conn, settings_store.PODCAST_GUID_KEY)
    if guid is None:
        raise RuntimeError("podcast GUID initialization failed")
    guid_epoch = settings_store.get_feed_guid_epoch(conn)

    last_build = identity.modified_at
    key = feed_auth.active_key(settings)
    etag = _feed_etag(identity)
    media_type = "application/rss+xml; charset=utf-8"
    headers = {
        "Last-Modified": format_datetime(last_build, usegmt=True),
        "Cache-Control": feed_auth.cache_control(
            conn, settings, settings.RSS_CACHE_MAX_AGE_SECONDS
        ),
        "ETag": etag,
    }
    # RFC 7232: when the request carries If-None-Match, ignore If-Modified-Since
    # and validate on the ETag alone.
    if if_none_match is not None:
        not_modified = _etag_matches(if_none_match, etag)
    else:
        not_modified = not identity.ims_ambiguous and _is_not_modified(
            if_modified_since, last_build
        )
    if not_modified:
        return Response(status_code=304, headers=headers)
    # HEAD: headers only, and skip the (gzip-able) render entirely.
    if request.method == "HEAD":
        return Response(status_code=200, headers=headers, media_type=media_type)

    database_path = conn.execute("PRAGMA database_list").fetchone()[2]
    cache_key = (
        database_path,
        identity.revision,
        identity.modified_at.isoformat(),
        identity.settings_fingerprint,
    )
    body = _rendered_feeds.get(cache_key)
    if body is None:
        rows = episodes.list_for_feed(conn)
        body = feed.render(
            rows,
            settings=settings,
            podcast_guid=guid,
            last_build=last_build,
            feed_guid_epoch=guid_epoch,
            feed_auth_key=key,
        )
        if len(body) <= _RENDERED_FEED_CACHE_BYTES:
            _rendered_feeds[cache_key] = body
            _rendered_feeds.move_to_end(cache_key)
            while (
                len(_rendered_feeds) > _RENDERED_FEED_CACHE_SIZE
                or sum(map(len, _rendered_feeds.values())) > _RENDERED_FEED_CACHE_BYTES
            ):
                _rendered_feeds.popitem(last=False)
    return Response(content=body, media_type=media_type, headers=headers)


def _feed_etag(identity: feed_revision.FeedIdentity) -> str:
    value = (
        f"{identity.revision}:{identity.modified_at.isoformat()}:"
        f"{identity.settings_fingerprint}"
    )
    return f'"{hashlib.sha256(value.encode()).hexdigest()[:32]}"'


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    candidates = {token.strip() for token in if_none_match.split(",")}
    opaque = etag.removeprefix("W/")
    return "*" in candidates or any(token.removeprefix("W/") == opaque for token in candidates)


def _feed_snapshot(
    conn: sqlite3.Connection, base_settings: Settings
) -> tuple[Settings, feed_revision.FeedIdentity]:
    while True:
        conn.execute("BEGIN")
        settings = runtime_settings.validated_overlay(
            base_settings, runtime_settings.get_all(conn)
        )
        identity = feed_revision.current(conn)
        if identity.settings_fingerprint == feed_revision.fingerprint(settings):
            return settings, identity
        conn.rollback()
        conn.execute("BEGIN IMMEDIATE")
        try:
            settings = runtime_settings.validated_overlay(
                base_settings, runtime_settings.get_all(conn)
            )
            feed_revision.identity(conn, settings)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


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
