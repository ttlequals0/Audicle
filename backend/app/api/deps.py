"""FastAPI dependencies for auth + CSRF enforcement.

Mutating admin endpoints (``PUT /prompt``, ``PUT /corrections``,
``POST /purge``) depend on ``require_admin`` which is a no-op when
``AUTH_ENABLED=false`` and otherwise asserts a valid session cookie + CSRF
header. Read-only endpoints (``GET /status``, ``GET /rss/rss.xml``,
``GET /media/*``) remain unauthenticated so podcast clients can subscribe.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.config import Settings, get_settings
from app.services import csrf

SESSION_KEY_USER = "audicle_user"


def require_admin(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Allow the request through only if either auth is disabled (single-
    operator install) or the session cookie + CSRF header are both valid.
    """

    if not settings.AUTH_ENABLED:
        return
    user = request.session.get(SESSION_KEY_USER)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    # Skip CSRF on safe methods. The header is a write-side defense; a
    # cookie-authenticated GET in a fresh tab (before the UI has loaded the
    # cookie into memory) should warm the cache, not 403.
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if not csrf.verify_token(
        request.headers.get(csrf.CSRF_HEADER_NAME),
        request.cookies.get(csrf.CSRF_COOKIE_NAME),
    ):
        raise HTTPException(status_code=403, detail="csrf token mismatch")
