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
from app.services import llm, runtime_settings
from app.version import __version__

logger = logging.getLogger("app.api.health")
router = APIRouter(tags=["health"])


@router.get("/health/live")
def health_live(request: Request) -> dict[str, Any]:
    started_at = getattr(request.app.state, "started_at", None)
    uptime_seconds = int(time.monotonic() - started_at) if started_at is not None else 0
    # base_url so the UI shows the configured public feed URL (BASE_URL), not
    # whatever host the browser happens to be on.
    return {
        "ok": True,
        "version": __version__,
        "uptime_seconds": uptime_seconds,
        "base_url": get_settings().BASE_URL,
    }


@router.get("/health/ready")
@router.get("/health")
async def health_ready(request: Request, response: Response) -> dict[str, Any]:
    # Apply the runtime_settings overlay (same as the pipeline / RSS) so the
    # probe reflects the operator's UI-set LLM model, Firecrawl/TTS URLs, etc. --
    # not the empty env defaults. Guarded: a DB failure here must not 500 the
    # health endpoint (the db check below records the failure instead).
    try:
        settings = runtime_settings.overlay(get_settings())
    except Exception:
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
    # Anthropic has no cheap unauthenticated probe (skip); the openai-compatible
    # family (openai-compatible / openrouter / ollama) all expose {base}/models,
    # resolved with the same base + auth the pipeline uses.
    llm_url: str | None = None
    llm_headers: dict[str, str] = {}
    if llm.is_openai_compatible_provider(settings.LLM_PROVIDER):
        base_url, api_key, extra_headers = llm.openai_compatible_connection(settings)
        llm_url = base_url or None
        llm_headers = dict(extra_headers)
        if api_key:
            llm_headers["Authorization"] = f"Bearer {api_key}"
    tts_result, firecrawl_result, llm_result, render_result = await asyncio.gather(
        _probe_tts_wrapper(settings.TTS_URL, 2.0),
        # Firecrawl's liveness is /v0/health/liveness (its /health path 404s);
        # the scrape API the pipeline uses is /v1/scrape on the same base.
        _probe_http(settings.FIRECRAWL_URL, "/v0/health/liveness", 2.0),
        _probe_http(llm_url, "/models", 2.0, llm_headers),
        _probe_render(settings.RENDER_URL, 2.0),
        return_exceptions=True,
    )
    tts_check, tts_detail = _coerce_tts(tts_result)
    checks["tts_wrapper"] = tts_check
    checks["firecrawl"] = _coerce_result(firecrawl_result)
    checks["llm"] = _coerce_result(llm_result)
    # Render is optional enrichment, so it is surfaced under components.render for
    # visibility but is NOT added to ``checks`` -- a down render sidecar must not
    # 503 readiness (the pipeline still produces episodes, just front-half only).
    render_check, render_detail = _coerce_tts(render_result)

    # Per build plan: aggregate component-level detail (wrapper version/torch/
    # device from its /health, LLM + Firecrawl reachability) alongside the local
    # app/python/ffmpeg versions.
    # _ffmpeg_version() runs a blocking subprocess; off-thread it so a wedged or
    # slow ffmpeg (failures aren't cached, so they re-run every probe) can't stall
    # the event loop for the full 2s timeout. Mirrors the worker retention sweep.
    ffmpeg_version = await asyncio.to_thread(_ffmpeg_version)
    components: dict[str, Any] = {
        "app": __version__,
        "python": platform.python_version(),
        "ffmpeg": ffmpeg_version,
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
        "render": {
            "url": settings.RENDER_URL or None,
            **render_detail,
            "reachable": _reachable(render_check),
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


async def _probe_http(
    base: str | None, path: str, timeout_secs: float, headers: dict[str, str] | None = None
) -> str:
    """``GET {base}{path}`` -> ``"ok"`` on 2xx, else a short reason."""

    if not base:
        return "skipped"
    url = f"{base.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout_secs) as client:
            r = await client.get(url, headers=headers or {})
        return "ok" if r.is_success else f"http_{r.status_code}"
    except httpx.HTTPError as exc:
        return f"unreachable_{type(exc).__name__}"


async def _probe_tts_wrapper(base: str | None, timeout_secs: float) -> tuple[str, dict[str, Any]]:
    """``GET {base}/health`` -> ``(check_status, component_detail)``.

    The wrapper reports its own ``engine``/``version``/``torch``/``device``/
    ``model_loaded`` plus the ASR-verify capability
    (``whisper_enabled``/``whisper_model``/``whisper_loaded``); surface that
    subset under ``components.tts_wrapper`` (``engine`` names the live backend,
    e.g. ``chatterbox``; the ``whisper_*`` fields let an operator confirm
    verification is actually loaded without reading the wrapper logs).
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
        for key in (
            "engine",
            "version",
            "torch",
            "device",
            "model_loaded",
            "whisper_enabled",
            "whisper_model",
            "whisper_loaded",
        )
        if key in payload
    }
    return ("ok" if r.is_success else f"http_{r.status_code}"), detail


async def _probe_render(base: str | None, timeout_secs: float) -> tuple[str, dict[str, Any]]:
    """``GET {base}/health/live`` -> ``(check_status, {version})``. The render
    sidecar reports only ``ok``/``version``; surface its version under
    ``components.render``. Skipped (and treated as reachable) when unconfigured."""

    if not base:
        return "skipped", {}
    url = f"{base.rstrip('/')}/health/live"
    try:
        async with httpx.AsyncClient(timeout=timeout_secs) as client:
            r = await client.get(url)
    except httpx.HTTPError as exc:
        return f"unreachable_{type(exc).__name__}", {}
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    detail = (
        {"version": payload["version"]}
        if isinstance(payload, dict) and "version" in payload
        else {}
    )
    return ("ok" if r.is_success else f"http_{r.status_code}"), detail
