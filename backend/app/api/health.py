"""Health endpoints.

- /health/live: liveness probe, no dependency checks.
- /health/ready: readiness probe, includes dependency status.
- /health: alias for /health/ready (kept for backward compatibility).

Later phases extend /health/ready with TTS wrapper, Firecrawl, LLM status.
"""

from __future__ import annotations

import logging
import platform
import time
from typing import Any

from fastapi import APIRouter, Request, Response, status

from app.config import get_settings
from app.core import database
from app.version import __version__

logger = logging.getLogger("app.api.health")
router = APIRouter(tags=["health"])


@router.get("/health/live")
def health_live() -> dict[str, Any]:
    return {"ok": True, "version": __version__}


@router.get("/health/ready")
@router.get("/health")
def health_ready(request: Request, response: Response) -> dict[str, Any]:
    settings = get_settings()
    checks: dict[str, str] = {}

    try:
        conn = database.connect(database.db_path(settings.DATA_DIR))
        try:
            conn.execute("SELECT 1").fetchone()
            checks["db"] = "ok"
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(
            "Readiness DB check failed",
            extra={"event": "health_db_error", "error": str(exc)},
            exc_info=True,
        )
        # Generic ``"error"`` rather than the raw exception string so
        # /health/ready (unauthenticated, conventionally polled by public
        # load balancers) doesn't leak DATA_DIR paths or errno text. The
        # full exception is in the WARN log above.
        checks["db"] = "error"

    started_at = getattr(request.app.state, "started_at", time.monotonic())
    body: dict[str, Any] = {
        "ok": all(value == "ok" for value in checks.values()),
        "version": __version__,
        "uptime_seconds": int(time.monotonic() - started_at),
        "components": {
            "app": __version__,
            "python": platform.python_version(),
        },
        "checks": checks,
    }

    if not body["ok"]:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return body
