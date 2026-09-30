"""FastAPI wrapper around a :class:`Renderer`.

The container starts via:

    xvfb-run -a uvicorn main:create_app --factory --host 0.0.0.0 --port 8000

``create_app`` takes an optional renderer so tests can inject a fake; in the image
it defaults to the Camoufox driver. The default is imported lazily so the app
(and its tests) stay importable without Camoufox installed.
"""

from __future__ import annotations

import logging
import os
from importlib import metadata
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from log_config import apply_logging, setup_logging
from renderer import RenderOptions, Renderer, RenderResult, default_runtime_config

setup_logging()
logger = logging.getLogger("render.main")


# Sidecar version, surfaced in /health/live so the main app's /health/ready can
# aggregate it into components.render.version. In the image the package is pip-installed,
# so its baked dist metadata (from render/pyproject.toml, which sync_version keeps in step
# with the repo-root VERSION) is the source -- no build arg to forget. In dev/tests the
# package isn't installed, so we walk up to the repo-root VERSION file instead.
def _render_version() -> str:
    try:
        version = metadata.version("audicle-render")
        if version:
            return version
    except Exception:  # a missing/damaged dist must not crash app import over a version
        pass
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "VERSION"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    return "0.0.0"


__version__ = _render_version()


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    url: str
    expand: bool = True
    # Address the operator configured for registration walls. Sent only when the
    # backend has already decided the page is gated; the renderer still checks the
    # page itself before typing it anywhere.
    email: str | None = None
    # The operator's subscriber session for this host (raw Cookie header), loaded into
    # the browser before navigation. Never logged.
    cookies: str | None = None
    # The caller's own wait, minus its margin. Caps the retry loop so the sidecar never
    # works past the point where the backend has stopped listening.
    budget_seconds: float | None = Field(default=None, gt=0)
    RENDER_NAV_TIMEOUT_MS: int | None = Field(default=None, ge=1000, le=180_000)
    RENDER_CLICK_TIMEOUT_MS: int | None = Field(default=None, ge=100, le=60_000)
    RENDER_GROW_WAIT_MS: int | None = Field(default=None, ge=0, le=30_000)
    RENDER_ATTEMPTS: int | None = Field(default=None, ge=1, le=10)
    RENDER_SETTLE_POLL_MS: int | None = Field(default=None, ge=50, le=5000)
    RENDER_SETTLE_QUIET_POLLS: int | None = Field(default=None, ge=1, le=20)
    RENDER_SETTLE_MAX_MS: int | None = Field(default=None, ge=100, le=60_000)
    RENDER_BUDGET_SECONDS: float | None = Field(default=None, gt=0, le=300)


class LoggingValues(BaseModel):
    model_config = ConfigDict(extra="forbid")
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None
    LOG_FORMAT: Literal["json", "text"] | None = None


class LoggingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: LoggingValues


def _default_renderer() -> Renderer:
    """Construct the real Camoufox renderer. Imported here, not at module top, so
    the app imports without Camoufox present (tests inject a fake instead)."""

    from camoufox_renderer import CamoufoxRenderer

    return CamoufoxRenderer()


def create_app(renderer: Renderer | None = None) -> FastAPI:
    app = FastAPI(title="Audicle render sidecar", version=__version__)
    app.state.renderer = renderer if renderer is not None else _default_renderer()
    log_defaults = {
        "LOG_LEVEL": os.environ.get("LOG_LEVEL", "INFO").upper(),
        "LOG_FORMAT": os.environ.get("LOG_FORMAT", "json"),
    }
    app.state.logging_config = log_defaults.copy()

    @app.get("/health/live")
    async def health_live() -> dict[str, object]:
        return {"ok": True, "version": __version__}

    @app.get("/runtime-config")
    async def runtime_config() -> dict[str, object]:
        defaults = {**default_runtime_config(), **log_defaults}
        return {"defaults": defaults, "effective": {**defaults, **app.state.logging_config}}

    @app.put("/runtime-config")
    async def update_runtime_config(body: LoggingRequest) -> dict[str, object]:
        values = {**log_defaults, **body.values.model_dump(exclude_none=True)}
        if values != app.state.logging_config:
            apply_logging(values["LOG_LEVEL"], values["LOG_FORMAT"])
            app.state.logging_config = values
        return await runtime_config()

    @app.post("/render")
    async def render(body: RenderRequest) -> dict[str, object]:
        options_values = {
            "nav_timeout_ms": body.RENDER_NAV_TIMEOUT_MS,
            "click_timeout_ms": body.RENDER_CLICK_TIMEOUT_MS,
            "grow_wait_ms": body.RENDER_GROW_WAIT_MS,
            "attempts": body.RENDER_ATTEMPTS,
            "settle_poll_ms": body.RENDER_SETTLE_POLL_MS,
            "settle_quiet_polls": body.RENDER_SETTLE_QUIET_POLLS,
            "settle_max_ms": body.RENDER_SETTLE_MAX_MS,
            "budget_seconds": body.RENDER_BUDGET_SECONDS,
        }
        options_values = {key: value for key, value in options_values.items() if value is not None}
        kwargs = {
            "email": body.email,
            "cookies": body.cookies,
            "budget_seconds": body.budget_seconds,
        }
        if options_values:
            kwargs["options"] = RenderOptions(**options_values)
        result: RenderResult = await app.state.renderer.render(body.url, body.expand, **kwargs)
        return {
            "status": result.status,
            "html": result.html,
            "clicks": result.clicks,
            "word_estimate": result.word_estimate,
        }

    return app
