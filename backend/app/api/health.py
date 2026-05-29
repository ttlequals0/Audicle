"""Health endpoints.

- /health/live: liveness probe, no dependency checks.
- /health/ready: readiness probe, includes dependency status.
- /health: alias for /health/ready (kept for backward compatibility).
"""

from __future__ import annotations

import asyncio
import logging
import platform
import subprocess
import time
from typing import Any

import httpx
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
async def health_ready(request: Request, response: Response) -> dict[str, Any]:
    settings = get_settings()
    checks: dict[str, str] = {}

    try:
        with database.connection(settings.DATA_DIR) as conn:
            conn.execute("SELECT 1").fetchone()
        checks["db"] = "ok"
    except Exception as exc:
        logger.warning(
            "Readiness DB check failed",
            extra={"event": "health_db_error", "error": str(exc)},
            exc_info=True,
        )
        checks["db"] = "error"

    # Fan probes out concurrently so one stuck upstream can't add its
    # timeout budget to the others'. return_exceptions=True ensures one
    # raising probe doesn't cancel the others and 500 the whole endpoint.
    llm_url = (
        settings.OPENAI_BASE_URL
        if settings.LLM_PROVIDER == "openai-compatible"
        else None
    )
    tts_result, firecrawl_result, llm_result = await asyncio.gather(
        _probe_tts_wrapper(settings.TTS_URL, 2.0),
        _probe_http(settings.FIRECRAWL_URL, "/health", 2.0),
        _probe_http(llm_url, "/models", 2.0),
        return_exceptions=True,
    )
    tts_check, tts_detail = _coerce_tts(tts_result)
    checks["tts_wrapper"] = tts_check
    checks["firecrawl"] = _coerce_result(firecrawl_result)
    checks["llm"] = _coerce_result(llm_result)

    # Per build plan: aggregate component-level detail (wrapper version/torch/
    # coqui_tts/device from its /health, LLM + Firecrawl reachability) alongside
    # the local app/python/ffmpeg versions.
    components: dict[str, Any] = {
        "app": __version__,
        "python": platform.python_version(),
        "ffmpeg": _ffmpeg_version(),
        "tts_wrapper": {**tts_detail, "reachable": _reachable(tts_check)},
        "firecrawl": {
            "url": settings.FIRECRAWL_URL,
            "reachable": _reachable(checks["firecrawl"]),
        },
        "llm": {
            "provider": settings.LLM_PROVIDER,
            "model": settings.LLM_MODEL,
            "base_url": llm_url,
            "reachable": _reachable(checks["llm"]),
        },
    }

    started_at = getattr(request.app.state, "started_at", time.monotonic())
    body: dict[str, Any] = {
        "ok": all(_reachable(v) for v in checks.values()),
        "version": __version__,
        "uptime_seconds": int(time.monotonic() - started_at),
        "components": components,
        "checks": checks,
    }

    if not body["ok"]:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return body


def _reachable(status: str) -> bool:
    """A probe is "reachable" when it answered ok or was deliberately skipped
    (no URL configured). Matches the top-level ``ok`` aggregation."""

    return status in {"ok", "skipped"}


def _coerce_result(value: Any) -> str:
    if isinstance(value, BaseException):
        return f"error_{type(value).__name__}"
    return str(value)


def _coerce_tts(value: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(value, BaseException):
        return f"error_{type(value).__name__}", {}
    return value


def _ffmpeg_version() -> str:
    """First-line ffmpeg version banner. Successful lookups are cached
    for the process lifetime via ``_ffmpeg_version_cached``; failures
    are deliberately NOT cached so a late PATH fix becomes visible."""

    cached = _ffmpeg_version_cached.get()
    if cached is not None:
        return cached
    try:
        out = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if out.returncode != 0:
            return "error"
        first_line = out.stdout.split("\n", 1)[0]
        parts = first_line.split()
        version = parts[2] if len(parts) >= 3 else "unknown"
        _ffmpeg_version_cached.set(version)
        return version
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "missing"


class _OnceCache:
    """Single-slot cache that only ever accepts a successful value;
    failure sentinels are re-tried on every call."""

    def __init__(self) -> None:
        self._value: str | None = None

    def get(self) -> str | None:
        return self._value

    def set(self, value: str) -> None:
        self._value = value


_ffmpeg_version_cached = _OnceCache()


async def _probe_http(base: str | None, path: str, timeout_secs: float) -> str:
    """``GET {base}{path}`` -> ``"ok"`` on 2xx, else a short reason."""

    if not base:
        return "skipped"
    url = f"{base.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout_secs) as client:
            r = await client.get(url)
        return "ok" if r.is_success else f"http_{r.status_code}"
    except httpx.HTTPError as exc:
        return f"unreachable_{type(exc).__name__}"


async def _probe_tts_wrapper(base: str | None, timeout_secs: float) -> tuple[str, dict[str, Any]]:
    """``GET {base}/health`` -> ``(check_status, component_detail)``.

    The wrapper reports its own ``version``/``torch``/``coqui_tts``/``device``/
    ``model_loaded``; surface that subset under ``components.tts_wrapper``.
    """

    if not base:
        return "skipped", {}
    url = f"{base.rstrip('/')}/health"
    try:
        async with httpx.AsyncClient(timeout=timeout_secs) as client:
            r = await client.get(url)
    except httpx.HTTPError as exc:
        return f"unreachable_{type(exc).__name__}", {}
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    detail = {
        key: payload[key]
        for key in ("version", "torch", "coqui_tts", "device", "model_loaded")
        if key in payload
    }
    return ("ok" if r.is_success else f"http_{r.status_code}"), detail
