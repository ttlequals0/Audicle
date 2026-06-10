"""``/api/v1/auth/*`` -- status, login, logout, set/change password.

Password-only admin auth (MinusPod pattern). The bcrypt hash lives in the
settings DB table, set via ``PUT /auth/password``. No password set = open
convenience mode (all admin endpoints allowed). Login is rate-limited by
slowapi and IP-lockout-protected. A CSRF token (double-submit cookie) is
issued on login/status for the UI to echo on mutating requests.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from slowapi import Limiter

from app.api.deps import SESSION_KEY_USER, client_ip, get_conn
from app.config import Settings, get_settings
from app.services import auth, csrf


def _client_id(request: Request) -> str:
    # Proxy-aware client identity, shared by the rate-limit bucket key and the
    # lockout identifier (see config.TRUST_PROXY_HEADERS), so the two can't drift.
    return client_ip(request, get_settings())


def _login_rate_limit() -> str:
    # Resolved per request (slowapi dynamic limit) so the LOGIN_RATE_LIMIT setting
    # (env / .env) actually drives the limiter instead of a hardcoded decorator value.
    return get_settings().LOGIN_RATE_LIMIT


# Per-IP rate limit fronting the bcrypt + lockout machinery.
_LOGIN_LIMITER = Limiter(key_func=_client_id)

router = APIRouter(prefix="/auth", tags=["auth"])


def _verify_or_raise(conn, *, password, request, settings, invalid_detail) -> None:
    """Run the password + lockout check, mapping failures to HTTP responses
    (423 Locked / 401 with ``invalid_detail``). Shared by login and the
    change-password flow."""

    try:
        auth.verify_login(
            conn, password=password, identifier=_client_id(request), settings=settings
        )
    except auth.LockedOutError as exc:
        raise HTTPException(
            status_code=423, detail=f"account locked until {exc.locked_until.isoformat()}"
        ) from exc
    except auth.InvalidCredentialsError as exc:
        raise HTTPException(status_code=401, detail=invalid_detail) from exc


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=200)


class PasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Required (verified) only when a password is already set. Empty new_password
    # removes the password (back to convenience mode).
    current_password: str | None = Field(default=None, max_length=200)
    new_password: str = Field(max_length=200)


class StatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password_set: bool
    authenticated: bool
    csrf_token: str | None = None


class AuthActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authenticated: bool
    password_set: bool
    csrf_token: str | None = None


@router.get("/status", response_model=StatusResponse)
async def get_status(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> StatusResponse:
    password_set = auth.is_password_set(conn)
    # Convenience mode (no password) reports authenticated=true.
    authenticated = (not password_set) or bool(request.session.get(SESSION_KEY_USER))
    csrf_token = request.cookies.get(csrf.CSRF_COOKIE_NAME) if authenticated else None
    return StatusResponse(
        password_set=password_set, authenticated=authenticated, csrf_token=csrf_token
    )


@router.post("/login", response_model=AuthActionResponse)
@_LOGIN_LIMITER.limit(_login_rate_limit)
async def post_login(
    request: Request,
    response: Response,
    payload: LoginRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> AuthActionResponse:
    if not auth.is_password_set(conn):
        raise HTTPException(status_code=400, detail="no password is set; auth is open")
    _verify_or_raise(
        conn,
        password=payload.password,
        request=request,
        settings=settings,
        invalid_detail="invalid password",
    )

    request.session[SESSION_KEY_USER] = "admin"
    token = _set_csrf_cookie(response, settings)
    return AuthActionResponse(authenticated=True, password_set=True, csrf_token=token)


@router.post("/logout")
async def post_logout(request: Request, response: Response) -> dict[str, bool]:
    request.session.clear()
    response.delete_cookie(csrf.CSRF_COOKIE_NAME)
    return {"authenticated": False}


@router.put("/password", response_model=AuthActionResponse)
async def put_password(
    request: Request,
    response: Response,
    payload: PasswordRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> AuthActionResponse:
    already_set = auth.is_password_set(conn)
    # Changing an existing password requires the current one; first-time set
    # in convenience mode does not.
    if already_set:
        if not payload.current_password:
            raise HTTPException(status_code=400, detail="current_password is required")
        _verify_or_raise(
            conn,
            password=payload.current_password,
            request=request,
            settings=settings,
            invalid_detail="current password is incorrect",
        )

    new_password = payload.new_password
    if new_password == "":
        auth.clear_password(conn)
        request.session.clear()
        response.delete_cookie(csrf.CSRF_COOKIE_NAME)
        return AuthActionResponse(authenticated=True, password_set=False)

    if len(new_password) < auth.MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"password must be at least {auth.MIN_PASSWORD_LENGTH} characters",
        )
    auth.set_password(conn, new_password)

    # Setting a password logs this session in.
    request.session[SESSION_KEY_USER] = "admin"
    token = _set_csrf_cookie(response, settings)
    return AuthActionResponse(authenticated=True, password_set=True, csrf_token=token)


def _set_csrf_cookie(response: Response, settings: Settings) -> str:
    token = csrf.issue_token()
    response.set_cookie(
        key=csrf.CSRF_COOKIE_NAME,
        value=token,
        max_age=settings.SESSION_COOKIE_MAX_AGE_SECONDS,
        secure=settings.SESSION_COOKIE_SECURE,
        # Deliberately readable by JS: the double-submit CSRF pattern requires the
        # SPA to read this token and echo it into the X-CSRF-Token header. Not a leak.
        httponly=False,
        samesite="lax",
    )
    return token
