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
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

from renderer import Renderer, RenderResult

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("render.main")


# Sidecar version, surfaced in /health/live so the main app's /health/ready can
# aggregate it into components.render.version. The repo-root VERSION file is the
# single source; in dev/tests we walk up to it, in the image the build passes it
# as AUDICLE_RENDER_VERSION (from `cat VERSION`) since the build context is render/.
def _render_version() -> str:
    env = os.environ.get("AUDICLE_RENDER_VERSION")
    if env:
        return env.strip()
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "VERSION"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    return "0.0.0"


__version__ = _render_version()


class RenderRequest(BaseModel):
    url: str
    expand: bool = True


def _default_renderer() -> Renderer:
    """Construct the real Camoufox renderer. Imported here, not at module top, so
    the app imports without Camoufox present (tests inject a fake instead)."""

    from camoufox_renderer import CamoufoxRenderer

    return CamoufoxRenderer()


def create_app(renderer: Renderer | None = None) -> FastAPI:
    app = FastAPI(title="Audicle render sidecar", version=__version__)
    app.state.renderer = renderer if renderer is not None else _default_renderer()

    @app.get("/health/live")
    async def health_live() -> dict[str, object]:
        return {"ok": True, "version": __version__}

    @app.post("/render")
    async def render(body: RenderRequest) -> dict[str, object]:
        result: RenderResult = await app.state.renderer.render(body.url, body.expand)
        return {
            "status": result.status,
            "html": result.html,
            "clicks": result.clicks,
            "word_estimate": result.word_estimate,
        }

    return app
