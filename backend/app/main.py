"""FastAPI application entry point."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import errors as error_handlers
from app.api.health import router as health_router
from app.api.media import router as media_router
from app.api.rss import router as rss_router
from app.api.v1.router import router as v1_router
from app.config import get_settings
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
    app = FastAPI(
        title="Audicle",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/v1/docs",
        openapi_url="/api/v1/openapi.json",
    )
    app.include_router(health_router)
    app.include_router(v1_router)
    app.include_router(rss_router)
    app.include_router(media_router)
    error_handlers.register(app)
    return app


app = create_app()
