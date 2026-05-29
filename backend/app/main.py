"""FastAPI application entry point."""

from __future__ import annotations

import logging
import re
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
from app.api.access_log import AccessLogMiddleware
from app.api.errors import envelope
from app.api.health import router as health_router
from app.api.media import router as media_router
from app.api.rss import router as rss_router
from app.api.v1.auth import _LOGIN_LIMITER
from app.api.v1.router import router as v1_router
from app.config import Settings, get_settings
from app.core import database
from app.services import settings_store
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
    # Always on, added last so it wraps the others: request_id is set + the timer
    # starts before any inner middleware/handler runs.
    app.add_middleware(AccessLogMiddleware)
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

    # Precompute, once at startup, the servable SPA root files as a
    # name -> on-disk Path map built by listing the build output. The request
    # path is only ever used as a dict KEY for lookup; the served Path comes
    # from the directory listing, so user input never constructs a filesystem
    # path (no traversal surface to guard).
    root_files: dict[str, Path] = {
        child.name: child
        for child in static_dir.iterdir()
        if child.is_file()
        and (child.name in _ROOT_STATIC_FILES or _WORKBOX_FILE_RE.fullmatch(child.name))
    }

    @app.get("/", include_in_schema=False)
    async def _ui_root() -> FileResponse:
        return FileResponse(index_path)

    # Catch-all for client-side routes that aren't ``/api/v1/*``,
    # ``/rss/*``, ``/media/*``, ``/health/*``, or under ``/assets``. The
    # router has already been mounted, so unmatched paths fall through to
    # this handler.
    @app.get("/{path:path}", include_in_schema=False)
    async def _ui_fallback(path: str) -> FileResponse:
        # Serve a known SPA root file by exact-name lookup; every other path
        # returns index.html so the React router handles it.
        served = root_files.get(path)
        if served is not None:
            return FileResponse(served)
        return FileResponse(index_path)


# SPA root files served by the catch-all (everything under /assets is mounted
# separately via StaticFiles). vite-plugin-pwa additionally emits a
# content-hashed ``workbox-<hex>.js`` runtime that sw.js imports.
_ROOT_STATIC_FILES = frozenset(
    {
        "favicon.svg",
        "manifest.webmanifest",
        "registerSW.js",
        "sw.js",
        "icon-192.png",
        "icon-512.png",
    }
)
_WORKBOX_FILE_RE = re.compile(r"workbox-[0-9a-f]+\.js")


def _resolve_session_secret(settings: Settings) -> str:
    """Session signing key: explicit env override, else a DB-persisted secret
    (auto-generated on first run) so sessions survive restarts. Falls back to an
    ephemeral key if the DB isn't ready yet (e.g. a stray pre-migration import).
    """

    if settings.SESSION_SECRET_KEY:
        return settings.SESSION_SECRET_KEY
    try:
        # run_migrations is idempotent + file-locked; ensures the settings table
        # exists before we read/init the persisted secret (middleware is built
        # before the lifespan's own migration call).
        database.run_migrations(settings.DATA_DIR)
        with database.connection(settings.DATA_DIR) as conn:
            return settings_store.get_or_init_session_secret(conn)
    except Exception:
        logging.getLogger("app.main").warning(
            "Could not persist a session secret; using an ephemeral key "
            "(sessions won't survive restart until DATA_DIR is writable)",
            extra={"event": "session_secret_ephemeral"},
            exc_info=True,
        )
        return secrets.token_urlsafe(64)


def _attach_session_middleware(app: FastAPI, settings: Settings) -> None:
    # SessionMiddleware is always attached so request.session exists for the auth
    # router / require_admin (which read it even in open convenience mode).
    app.add_middleware(
        SessionMiddleware,
        secret_key=_resolve_session_secret(settings),
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
