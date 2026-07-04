"""``GET/POST /api/v1/feed-auth`` -- manage the optional authenticated-feeds key.

Storage lives in the runtime-settings overlay (``FEED_AUTH_ENABLED``,
``FEED_AUTH_KEY``), but a server-generated secret with lazy generation, rotation,
and 409-while-disabled semantics does not fit the generic settings form, so it
gets these purpose-built endpoints. The key is returned in the clear -- the
operator must read it to build the keyed subscribe URL. Admin session + CSRF come
from the router-level ``require_admin`` gate in ``router.py``.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from app.api.deps import get_conn
from app.api.v1.auth import _LOGIN_LIMITER
from app.config import Settings, get_settings
from app.services import feed, feed_auth, runtime_settings

router = APIRouter(tags=["feed-auth"])


class FeedAuthStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    # Returned in the clear (not masked): the operator needs it to subscribe apps.
    # None until a key has been generated (first enable).
    key: str | None
    # The ready-to-copy keyed feed URL, or None when auth is off / no key yet.
    keyed_feed_url: str | None


class FeedAuthToggle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


def _status(settings: Settings) -> FeedAuthStatus:
    key = settings.FEED_AUTH_KEY or None
    keyed_url = feed.self_feed_url(settings, key) if (settings.FEED_AUTH_ENABLED and key) else None
    return FeedAuthStatus(enabled=settings.FEED_AUTH_ENABLED, key=key, keyed_feed_url=keyed_url)


def _effective_status(settings: Settings) -> FeedAuthStatus:
    """Status from the current effective settings (one overlay read)."""

    return _status(runtime_settings.overlay(settings))


@router.get("/feed-auth", response_model=FeedAuthStatus)
async def get_feed_auth(
    settings: Annotated[Settings, Depends(get_settings)],
) -> FeedAuthStatus:
    return _effective_status(settings)


@router.post("/feed-auth", response_model=FeedAuthStatus)
async def set_feed_auth_enabled(
    body: FeedAuthToggle,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FeedAuthStatus:
    """Enable or disable authenticated feeds.

    Enabling with no stored key mints one (retained across a later disable, so
    re-enabling reuses the same key rather than breaking every already-updated
    app again). ``set_if_absent`` is atomic, so two concurrent enables agree on
    one key instead of racing to different values -- the status then returns the
    key the operator can actually subscribe with. ``body.enabled`` is a real bool
    via Pydantic, so a stringly-typed ``"false"`` can't slip through as True.
    """

    if body.enabled:
        runtime_settings.set_if_absent(conn, "FEED_AUTH_KEY", feed_auth.generate_key())
    runtime_settings.set_value(conn, "FEED_AUTH_ENABLED", body.enabled)
    return _effective_status(settings)


@router.post("/feed-auth/regenerate", response_model=FeedAuthStatus)
@_LOGIN_LIMITER.limit("3/hour")
async def regenerate_feed_auth_key(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FeedAuthStatus:
    """Rotate the key. 409 while disabled -- rotating a dormant key is invisible
    and surprising on re-enable. The old key stops working the moment the new one
    is stored, so every subscribed app must re-subscribe with the new URL."""

    enabled, _key = feed_auth.effective_auth(conn, settings)
    if not enabled:
        raise HTTPException(
            status_code=409, detail="enable authenticated feeds before rotating the key"
        )
    runtime_settings.set_value(conn, "FEED_AUTH_KEY", feed_auth.generate_key())
    return _effective_status(settings)
