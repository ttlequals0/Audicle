"""FastAPI application entry point."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from slowapi.errors import RateLimitExceeded
from starlette.middleware.sessions import SessionMiddleware

from app.api import errors as error_handlers
from app.api.health import router as health_router
from app.api.media import router as media_router
from app.api.rss import router as rss_router
from app.api.v1.router import router as v1_router
from app.config import Settings, get_settings
from app.startup import bootstrap
from app.version import __version__


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    bootstrap(settings, process_label="web")
    app.state.started_at = time.monotonic()
    yield
    logging.getLogger("app.main").info("Audicle shutting down", extra={"event": "app_stopping"})


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Audicle",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/v1/docs",
        openapi_url="/api/v1/openapi.json",
    )
    _attach_session_middleware(app, settings)
    _attach_rate_limiter(app, settings)
    app.include_router(health_router)
    app.include_router(v1_router)
    app.include_router(rss_router)
    app.include_router(media_router)
    error_handlers.register(app)
    return app


def _attach_session_middleware(app: FastAPI, settings: Settings) -> None:
    # When auth is disabled we still attach SessionMiddleware with a random
    # ephemeral key so the session attribute exists on Request (the auth
    # router and require_admin read from request.session even when auth is
    # off, in which case both reads return None). The ephemeral key means
    # the cookie is unforgeable across restarts but no operator state
    # depends on it persisting.
    if settings.AUTH_ENABLED and settings.SESSION_SECRET_KEY:
        secret_key = settings.SESSION_SECRET_KEY
    else:
        import secrets as _secrets

        secret_key = _secrets.token_urlsafe(64)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        session_cookie="audicle_session",
        max_age=settings.SESSION_COOKIE_MAX_AGE_SECONDS,
        same_site="lax",
        https_only=settings.SESSION_COOKIE_SECURE,
    )


def _attach_rate_limiter(app: FastAPI, settings: Settings) -> None:
    # The auth router instantiates its own module-level limiter so the
    # decorator can resolve at import time; we hook the same instance into
    # ``app.state.limiter`` so slowapi's request middleware can find it.
    from app.api.v1.auth import _LOGIN_LIMITER

    app.state.limiter = _LOGIN_LIMITER

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_handler(_request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=429,
            content={"error": "rate limit exceeded", "status": 429},
        )

    _ = settings  # LOGIN_RATE_LIMIT is currently advisory; see auth.py


app = create_app()
