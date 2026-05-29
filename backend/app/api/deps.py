"""FastAPI dependencies for auth + CSRF enforcement.

Mutating admin endpoints depend on ``require_admin``. When no admin password is
set (the open convenience mode for a single-operator localhost install) it is a
no-op; once a password is set it requires a valid session cookie plus a CSRF
header on mutating methods. Read-only public routes (``/rss/*``, ``/media/*``)
stay unauthenticated so podcast clients can subscribe.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.config import Settings, get_settings
from app.core import database
from app.services import auth, csrf

SESSION_KEY_USER = "audicle_user"


def require_admin(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Allow the request only in convenience mode (no password set) or when the
    session cookie + CSRF header are both valid."""

    with database.connection(settings.DATA_DIR) as conn:
        password_set = auth.is_password_set(conn)
    if not password_set:
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
