"""FastAPI application entry point."""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from starlette.middleware.sessions import SessionMiddleware

from app.api import errors as error_handlers
from app.api.errors import envelope
from app.api.health import router as health_router
from app.api.media import router as media_router
from app.api.rss import router as rss_router
from app.api.v1.auth import _LOGIN_LIMITER
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
    _mount_static_ui(app)
    return app


def _mount_static_ui(app: FastAPI) -> None:
    """Serve the Vite-built SPA at ``/`` and fall back to ``index.html`` for
    client-side routes so deep links work (``/feed``, ``/settings``, etc.).

    The Dockerfile builds ``frontend/dist`` and copies it to
    ``/app/static/ui`` inside the runtime image. In dev (``uv run uvicorn``
    against a checkout) the directory may not exist -- skip the mount in
    that case so the API alone still boots.
    """

    static_dir = Path(__file__).resolve().parent.parent / "static" / "ui"
    if not static_dir.exists():
        return
    index_path = static_dir / "index.html"
    # Serve the static files (with proper Cache-Control via StaticFiles).
    app.mount(
        "/assets",
        StaticFiles(directory=static_dir / "assets"),
        name="ui-assets",
    )

    @app.get("/", include_in_schema=False)
    async def _ui_root() -> FileResponse:
        return FileResponse(index_path)

    # Catch-all for client-side routes that aren't ``/api/v1/*``,
    # ``/rss/*``, ``/media/*``, ``/health/*``, or under ``/assets``. The
    # router has already been mounted, so unmatched paths fall through to
    # this handler.
    @app.get("/{path:path}", include_in_schema=False)
    async def _ui_fallback(path: str) -> FileResponse:
        # If the requested path is a real static file (favicon, manifest,
        # service worker, etc.), serve it; otherwise return index.html so
        # the React router can pick up the route.
        candidate = static_dir / path
        if candidate.is_file() and _is_within(candidate, static_dir):
            return FileResponse(candidate)
        return FileResponse(index_path)


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _attach_session_middleware(app: FastAPI, settings: Settings) -> None:
    # When auth is disabled we still attach SessionMiddleware with a random
    # ephemeral key so the session attribute exists on Request (the auth
    # router and require_admin read from request.session even when auth is
    # off, in which case both reads return None). The ephemeral key means
    # the cookie is unforgeable across restarts but no operator state
    # depends on it persisting.
    # ``_validate_auth`` refuses to start with AUTH_ENABLED=true and no
    # SESSION_SECRET_KEY, so the random-key path is only reached when auth
    # is off -- the session contents are not security-relevant in that mode.
    if settings.AUTH_ENABLED and settings.SESSION_SECRET_KEY:
        secret_key = settings.SESSION_SECRET_KEY
    else:
        secret_key = secrets.token_urlsafe(64)
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
    # decorator can resolve at import time; hook the same instance into
    # ``app.state.limiter`` so slowapi's request middleware finds it.
    app.state.limiter = _LOGIN_LIMITER

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_handler(_request, exc):
        return envelope(status=429, error="rate limit exceeded")

    _ = settings  # LOGIN_RATE_LIMIT is currently advisory; see auth.py


app = create_app()
