"""``/api/v1/auth/*`` -- login, logout, status.

Login is rate-limited by slowapi (``LOGIN_RATE_LIMIT``) to slow brute-force
attempts independently of the lockout window. The session cookie is
managed by Starlette's ``SessionMiddleware`` (signed with
``SESSION_SECRET_KEY``). A CSRF token is issued alongside the session so
the admin UI can echo it on every mutating request.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.api.deps import SESSION_KEY_USER
from app.config import Settings, get_settings
from app.core import database
from app.services import auth, csrf

# Per-IP rate limit fronting the bcrypt + lockout machinery. Operators
# override via LOGIN_RATE_LIMIT in the env. The Limiter is module-level so
# the @limit decorator can read it at import time; the actual limit string
# is looked up at decoration time from a default. Per-request override
# would require restructuring slowapi, which isn't worth the churn for a
# single-admin install.
_LOGIN_LIMITER = Limiter(key_func=get_remote_address)


router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class LoginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logged_in: bool
    username: str
    csrf_token: str


class StatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auth_enabled: bool
    logged_in: bool
    username: str | None = None
    csrf_token: str | None = None


@router.post("/login", response_model=LoginResponse)
@_LOGIN_LIMITER.limit("10/minute")
async def post_login(
    request: Request,
    response: Response,
    payload: LoginRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> LoginResponse:
    if not settings.AUTH_ENABLED:
        raise HTTPException(status_code=400, detail="auth is not enabled on this instance")

    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        try:
            auth.verify_credentials(
                conn,
                username=payload.username,
                password=payload.password,
                settings=settings,
            )
        except auth.LockedOutError as exc:
            raise HTTPException(
                status_code=423,
                detail=f"account locked until {exc.locked_until.isoformat()}",
            ) from exc
        except auth.InvalidCredentialsError as exc:
            raise HTTPException(status_code=401, detail="invalid username or password") from exc
    finally:
        conn.close()

    request.session[SESSION_KEY_USER] = settings.ADMIN_USERNAME
    token = csrf.issue_token()
    response.set_cookie(
        key=csrf.CSRF_COOKIE_NAME,
        value=token,
        max_age=settings.SESSION_COOKIE_MAX_AGE_SECONDS,
        secure=settings.SESSION_COOKIE_SECURE,
        httponly=False,  # the UI must read this to echo into X-CSRF-Token
        samesite="lax",
    )
    return LoginResponse(logged_in=True, username=settings.ADMIN_USERNAME, csrf_token=token)


@router.post("/logout")
async def post_logout(request: Request, response: Response) -> dict[str, bool]:
    request.session.clear()
    response.delete_cookie(csrf.CSRF_COOKIE_NAME)
    return {"logged_out": True}


@router.get("/status", response_model=StatusResponse)
async def get_status(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> StatusResponse:
    user = request.session.get(SESSION_KEY_USER)
    csrf_token = request.cookies.get(csrf.CSRF_COOKIE_NAME)
    return StatusResponse(
        auth_enabled=settings.AUTH_ENABLED,
        logged_in=bool(user),
        username=user if user else None,
        csrf_token=csrf_token if user else None,
    )
