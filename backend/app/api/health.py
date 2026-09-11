"""Health endpoints.

- /health/live: liveness probe, no dependency checks.
- /health/ready: podcast-serving readiness, checks local storage only.
- /health: alias for /health/ready (kept for backward compatibility).
- /health/ingestion: selected processing dependency status.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import subprocess
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response, status

from app.api.v1 import llm as llm_api
from app.config import Settings, get_settings
from app.core import database
from app.core.paths import media_dir
from app.services import runtime_settings
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
    media_path = media_dir(settings)
    checks["media"] = (
        "ok" if media_path.is_dir() and os.access(media_path, os.R_OK | os.X_OK) else "error"
    )
    body: dict[str, Any] = {
        "ok": all(value == "ok" for value in checks.values()),
        "version": __version__,
        "uptime_seconds": int(
            time.monotonic() - getattr(request.app.state, "started_at", time.monotonic())
        ),
        "checks": checks,
    }
    if not body["ok"]:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return body


@router.get("/health/ingestion")
async def health_ingestion(request: Request, response: Response) -> dict[str, Any]:
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
    # Probe each selected dependency using the same base and authentication
    # contract as the corresponding client.
    wrapper_selected = settings.TTS_BACKEND == "wrapper" or (
        settings.verification_enabled and settings.WHISPER_BACKEND == "wrapper"
    )
    remote_tts_url = settings.TTS_API_BASE_URL if settings.TTS_BACKEND == "openai-api" else None
    remote_asr_url = (
        settings.WHISPER_API_BASE_URL
        if settings.verification_enabled and settings.WHISPER_BACKEND == "openai-api"
        else None
    )
    (
        tts_result,
        firecrawl_result,
        llm_result,
        render_result,
        remote_tts,
        remote_asr,
    ) = await asyncio.gather(
        _probe_tts_wrapper(settings.TTS_URL if wrapper_selected else None, 2.0),
        # Firecrawl's liveness is /v0/health/liveness (its /health path 404s);
        # the scrape API the pipeline uses is /v1/scrape on the same base.
        _probe_http(
            settings.FIRECRAWL_URL if settings.EXTRACTION_ENGINE == "firecrawl" else None,
            "/v0/health/liveness",
            2.0,
        ),
        _probe_llm_provider(settings, 2.0),
        _probe_render(settings.RENDER_URL, 2.0),
        _probe_http(remote_tts_url, "/models", 2.0, _bearer(settings.TTS_API_KEY)),
        _probe_http(remote_asr_url, "/models", 2.0, _bearer(settings.WHISPER_API_KEY)),
        return_exceptions=True,
    )
    tts_check, tts_detail = _coerce_tts(tts_result)
    if wrapper_selected:
        checks["tts_wrapper"] = tts_check
    if settings.EXTRACTION_ENGINE == "firecrawl":
        checks["firecrawl"] = _coerce_result(firecrawl_result)
    checks["llm"] = _coerce_result(llm_result)
    if settings.TTS_BACKEND == "openai-api":
        checks["tts_remote"] = _coerce_result(remote_tts) if remote_tts_url else "unconfigured"
    if settings.verification_enabled and settings.WHISPER_BACKEND == "openai-api":
        checks["asr_remote"] = _coerce_result(remote_asr) if remote_asr_url else "unconfigured"
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
            "selected": settings.EXTRACTION_ENGINE == "firecrawl",
            "status": _coerce_result(firecrawl_result),
            "reachable": _reachable(_coerce_result(firecrawl_result)),
        },
        "llm": {
            "provider": settings.LLM_PROVIDER,
            "model": settings.LLM_MODEL,
            "reachable": _reachable(checks["llm"]),
        },
        "render": {
            "url": settings.RENDER_URL or None,
            **render_detail,
            "reachable": _reachable(render_check),
        },
        "tts_remote": {
            "url": remote_tts_url,
            "status": _coerce_result(remote_tts),
            "reachable": _reachable(_coerce_result(remote_tts)),
        },
        "asr_remote": {
            "url": remote_asr_url,
            "status": _coerce_result(remote_asr),
            "reachable": _reachable(_coerce_result(remote_asr)),
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
    """True only when a configured dependency answered successfully."""

    return status == "ok"


def _bearer(api_key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


async def _probe_llm_provider(settings: Settings, timeout: float) -> str:
    request = llm_api.ConnectionTestRequest(provider=settings.LLM_PROVIDER)
    try:
        url, headers, kind = llm_api.provider_probe_request(settings, request)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers)
        if not response.is_success:
            return f"error_status_{response.status_code}"
        return "ok" if llm_api.valid_provider_probe_response(kind, response) else "error_response"
    except ValueError:
        return "unconfigured"
    except httpx.HTTPError as exc:
        return f"error_{type(exc).__name__}"


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
