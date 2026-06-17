"""FastAPI dependencies for auth + CSRF enforcement.

Mutating admin endpoints depend on ``require_admin``. When no admin password is
set (the open convenience mode for a single-operator localhost install) it is a
no-op; once a password is set it requires a valid session cookie plus a CSRF
header on mutating methods. Read-only public routes (``/rss/*``, ``/media/*``)
stay unauthenticated so podcast clients can subscribe.
"""

from __future__ import annotations

import ipaddress
import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.config import Settings, get_settings
from app.core import database
from app.services import auth, csrf, voices

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


def require_voice_loaded() -> None:
    """Reject a new job when no reference voice is loaded. Slots-only model: a job
    has nothing to narrate with until at least one slot is filled, so submit/upload
    fail fast with 400 instead of queuing a job that can only die at the TTS stage."""

    if not voices.filled_slots():
        raise HTTPException(
            status_code=400,
            detail="no reference voice is loaded; add at least one voice slot in Settings first",
        )
