"""FastAPI dependencies for auth + CSRF enforcement.

Mutating admin endpoints depend on ``require_admin``. When no admin password is
set (the open convenience mode for a single-operator localhost install) it is a
no-op; once a password is set it requires a valid session cookie plus a CSRF
header on mutating methods. Read-only public routes (``/rss/*``, ``/media/*``)
stay unauthenticated so podcast clients can subscribe.
"""

from __future__ import annotations

import ipaddress
import logging
import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.config import Settings, get_settings
from app.core import database
from app.services import auth, csrf, feed_auth, runtime_settings, voices

logger = logging.getLogger("app.api.deps")

SESSION_KEY_USER = "audicle_user"


def get_conn(
    settings: Annotated[Settings, Depends(get_settings)],
) -> Iterator[sqlite3.Connection]:
    """Request-scoped SQLite connection.

    FastAPI caches dependency results within a request, so ``require_admin`` and
    the handler that both depend on this share one connection -- a single open per
    request instead of one for the auth check plus one for the handler. Opened with
    ``check_same_thread=False`` because FastAPI may resolve this on a threadpool
    thread and run the handler on the event loop (and vice versa); the connection
    is per-request and never used concurrently.
    """

    with database.connection(settings.DATA_DIR, check_same_thread=False) as conn:
        yield conn


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def client_ip(request: Request, settings: Settings) -> str:
    """Resolve the client IP for rate-limiting and lockout.

    When ``TRUST_PROXY_HEADERS`` is set, take the ``X-Forwarded-For`` entry
    ``TRUSTED_PROXY_HOPS`` from the right -- the hop our own proxy appended --
    rather than the client-controlled leftmost value, which an attacker could
    spoof to evade the limit. Falls back to the socket peer when the header is
    absent, the flag is off, or the candidate isn't a valid IP.
    """

    if settings.TRUST_PROXY_HEADERS:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            hops = settings.TRUSTED_PROXY_HOPS
            if len(parts) >= hops:
                candidate = parts[-hops]
                if _is_ip(candidate):
                    return candidate
    return request.client.host if request.client else "unknown"


def require_admin(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> None:
    """Allow the request only in convenience mode (no password set) or when the
    session cookie + CSRF header are both valid."""

    if not auth.is_password_set(conn):
        return
    if not request.session.get(SESSION_KEY_USER):
        raise HTTPException(status_code=401, detail="login required")
    # Safe methods don't need the CSRF header (it's a write-side defense).
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if not csrf.verify_token(
        request.headers.get(csrf.CSRF_HEADER_NAME),
        request.cookies.get(csrf.CSRF_COOKIE_NAME),
    ):
        raise HTTPException(status_code=403, detail="csrf token mismatch")


def get_effective_settings(
    settings: Annotated[Settings, Depends(get_settings)],
) -> Settings:
    """The env/code ``Settings`` with the ``runtime_settings`` overlay applied,
    resolved once per request. FastAPI caches the dependency, so multiple
    consumers -- the feed-key guard and the route handler -- share one overlay
    read instead of each calling ``runtime_settings.overlay()`` itself (what the
    ``overlay`` docstring prescribes)."""

    return runtime_settings.overlay(settings)


def require_feed_key(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Gate the public feed/media surface on the global feed key.

    No-op while ``FEED_AUTH_ENABLED`` is off; when on, 401 unless the request
    carries the active key. The key comes from ``?key=`` (RSS, mp3, vtt, txt)
    or, for cover art, the ``<episode_id>-<key>`` path token. Fails closed if
    enabled with no stored key. Reads only the two feed-auth rows off the
    request connection (``feed_auth.effective_auth``) -- no full overlay on the
    hot media path -- per request, so a rotation applies with no restart. HEAD
    is covered (Starlette serves it through the GET view). Router-level on the
    rss + media routers only; the admin API, /health, and the SPA are not gated.

    An authenticated admin is exempt: the Feed/Settings UI links to ``/media/...``
    relatively, so the browser sends the session cookie, and the logged-in operator
    should not have to carry the feed key to play audio or open a transcript in their
    own dashboard. Public podcast clients have no session and still need the key.
    """

    enabled, expected = feed_auth.effective_auth(conn, settings)
    if not enabled:
        return
    if request.session.get(SESSION_KEY_USER):
        return
    _, cover_key = feed_auth.split_cover_token(request.path_params.get("episode_id"))
    supplied = request.query_params.get("key") or cover_key
    if not feed_auth.verify(supplied, expected):
        logger.warning(
            "feed key rejected: %s %s [%s]",
            request.method,
            request.url.path,
            client_ip(request, settings),
        )
        raise HTTPException(status_code=401, detail="feed key required")


def require_voice_loaded(
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Reject a new job when no reference voice is loaded. Slots-only model: a job
    has nothing to narrate with until at least one slot is filled, so submit/upload
    fail fast with 400 instead of queuing a job that can only die at the TTS stage.

    Reference-voice slots are a bundled-wrapper concept. A remote
    OpenAI-compatible server manages its own voices and is told which to use by
    name (``TTS_API_VOICE``), so this gate would block every submission on a
    deployment that has no local slots and does not need any."""

    # Read the single key off the request's existing connection rather than
    # building a whole overlay: get_effective_settings opens its own connection,
    # and submit is asserted to open exactly one per request.
    backend = runtime_settings.get_all(conn).get("TTS_BACKEND", settings.TTS_BACKEND)
    if backend == "openai-api":
        return

    if not voices.filled_slots():
        raise HTTPException(
            status_code=400,
            detail="no reference voice is loaded; add at least one voice slot in Settings first",
        )
