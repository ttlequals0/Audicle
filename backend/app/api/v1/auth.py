"""``/api/v1/auth/*`` -- status, login, logout, set/change password.

Password-only admin auth (MinusPod pattern). The bcrypt hash lives in the
settings DB table, set via ``PUT /auth/password``. No password set = open
convenience mode (all admin endpoints allowed). Login is rate-limited by
slowapi and IP-lockout-protected. A CSRF token (double-submit cookie) is
issued on login/status for the UI to echo on mutating requests.
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

# Per-IP rate limit fronting the bcrypt + lockout machinery (LOGIN_RATE_LIMIT).
_LOGIN_LIMITER = Limiter(key_func=get_remote_address)

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_id(request: Request) -> str:
    return request.client.host if request.client else "unknown"


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
    settings: Annotated[Settings, Depends(get_settings)],
) -> StatusResponse:
    with database.connection(settings.DATA_DIR) as conn:
        password_set = auth.is_password_set(conn)
    # Convenience mode (no password) reports authenticated=true.
    authenticated = (not password_set) or bool(request.session.get(SESSION_KEY_USER))
    csrf_token = request.cookies.get(csrf.CSRF_COOKIE_NAME) if authenticated else None
    return StatusResponse(
        password_set=password_set, authenticated=authenticated, csrf_token=csrf_token
    )


@router.post("/login", response_model=AuthActionResponse)
@_LOGIN_LIMITER.limit("10/minute")
async def post_login(
    request: Request,
    response: Response,
    payload: LoginRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthActionResponse:
    with database.connection(settings.DATA_DIR) as conn:
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
) -> AuthActionResponse:
    with database.connection(settings.DATA_DIR) as conn:
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
        httponly=False,  # the UI reads this to echo into X-CSRF-Token
        samesite="lax",
    )
    return token
