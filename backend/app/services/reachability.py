"""Startup reachability checks (advisory).

Each external dependency the pipeline needs (Firecrawl + LLM + TTS wrapper) is
probed when the worker starts. Results are logged and surfaced in /health/ready,
but a failure never blocks startup: the worker enters its poll loop regardless,
and a job that hits a down dependency fails that stage with a clear error.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.services import llm

logger = logging.getLogger("app.services.reachability")


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


async def check_firecrawl(settings: Settings, *, timeout: float = 5.0) -> CheckResult:
    """Probe Firecrawl with whichever health path it exposes.

    Self-hosted Firecrawl historically responds at ``GET /`` with a JSON banner,
    while some versions expose ``GET /v1/health``. We try the well-known
    endpoints in order and treat the first 2xx as healthy. A non-2xx from the
    last candidate is reported as the failure detail.
    """

    base = settings.FIRECRAWL_URL.rstrip("/")
    candidates = (f"{base}/v1/health", f"{base}/health", f"{base}/")
    last_detail = "no endpoint responded"

    async with httpx.AsyncClient(timeout=timeout) as client:
        for endpoint in candidates:
            try:
                response = await client.get(endpoint)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_detail = f"unreachable ({exc.__class__.__name__}: {exc})"
                continue
            if response.is_success:
                return CheckResult(
                    name="firecrawl",
                    ok=True,
                    detail=f"HTTP {response.status_code} from {endpoint.split(base)[1] or '/'}",
                )
            last_detail = f"HTTP {response.status_code}: {response.text[:120]}"

    return CheckResult(name="firecrawl", ok=False, detail=last_detail)


async def check_llm(settings: Settings, *, timeout: float = 5.0) -> CheckResult:
    """Probe the configured LLM endpoint.

    For ``openai-compatible``: GET ``OPENAI_BASE_URL/models`` -- the well-known
    list-models endpoint, which every Ollama / vLLM / LM Studio /
    OpenAI-compatible server exposes.

    For ``anthropic``: no cheap unauthenticated probe exists, so we only
    validate that ``ANTHROPIC_API_KEY`` is present (the build plan calls this
    out explicitly: "Skip; Anthropic API has no cheap health check").
    """

    if settings.LLM_PROVIDER == "anthropic":
        if not settings.ANTHROPIC_API_KEY:
            return CheckResult(name="llm", ok=False, detail="ANTHROPIC_API_KEY is not set")
        return CheckResult(name="llm", ok=True, detail="anthropic provider; API key present")

    # openai-compatible / openrouter / ollama all expose {base}/models; resolve
    # the base + auth the same way the pipeline does so each provider probes its
    # own endpoint with its own key/headers.
    base_url, api_key, extra_headers = llm.openai_compatible_connection(settings)
    base = base_url.rstrip("/")
    if not base:
        return CheckResult(name="llm", ok=False, detail="LLM base URL is not set")

    endpoint = f"{base}/models"
    headers = dict(extra_headers)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(endpoint, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            return CheckResult(
                name="llm",
                ok=False,
                detail=f"unreachable ({exc.__class__.__name__}: {exc})",
            )
    if response.is_success:
        return CheckResult(name="llm", ok=True, detail=f"HTTP {response.status_code}")
    return CheckResult(
        name="llm",
        ok=False,
        detail=f"HTTP {response.status_code}: {response.text[:120]}",
    )


async def check_tts(settings: Settings) -> CheckResult:
    """Probe the TTS wrapper's ``/health`` endpoint with a startup grace period.

    The wrapper takes 10-30s to load XTTS-v2 + compute speaker embeddings on a
    cold container start. We poll for up to
    ``TTS_REACHABILITY_GRACE_SECONDS`` (default 60s) with a
    ``TTS_REACHABILITY_PROBE_TIMEOUT`` per attempt, returning the first
    ``model_loaded: true`` response.
    """

    endpoint = f"{settings.TTS_URL.rstrip('/')}/health"
    deadline = time.monotonic() + settings.TTS_REACHABILITY_GRACE_SECONDS
    per_probe = httpx.Timeout(settings.TTS_REACHABILITY_PROBE_TIMEOUT)
    last_detail = "no probe completed"
    attempt = 0

    # One AsyncClient for the whole grace window so successive probes share
    # the connection pool instead of paying TCP/TLS setup on every iteration.
    async with httpx.AsyncClient(timeout=per_probe) as client:
        while True:
            attempt += 1
            try:
                response = await client.get(endpoint)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_detail = f"unreachable ({exc.__class__.__name__}: {exc})"
            else:
                # The model being loaded is what makes TTS reachable. A 503 with
                # model_loaded=true just means no reference voice is committed yet
                # (the operator uploads one via the UI), so accept it rather than
                # blocking the worker on a missing voice.
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                if isinstance(body, dict) and body.get("model_loaded"):
                    return CheckResult(
                        name="tts",
                        ok=True,
                        detail=(
                            f"HTTP {response.status_code} after {attempt} probe(s); "
                            f"model_loaded=true, reference_loaded={body.get('reference_loaded', False)}"
                        ),
                    )
                last_detail = (
                    f"HTTP {response.status_code}, model_loaded=false "
                    f"(reference_loaded={body.get('reference_loaded') if isinstance(body, dict) else 'n/a'})"
                )

            # Per-probe debug log so a stalled cold-start is visible in Loki
            # instead of 60 silent seconds.
            logger.debug(
                "TTS reachability probe",
                extra={
                    "event": "reachability_probe",
                    "phase": "startup",
                    "check": "tts",
                    "attempt": attempt,
                    "detail": last_detail,
                },
            )

            if time.monotonic() >= deadline:
                return CheckResult(
                    name="tts",
                    ok=False,
                    detail=(
                        f"grace period ({settings.TTS_REACHABILITY_GRACE_SECONDS}s) expired "
                        f"after {attempt} probe(s): {last_detail}"
                    ),
                )
            # Back off a tick before the next probe so we don't hot-spin while
            # the wrapper is still loading.
            await asyncio.sleep(min(1.0, max(0.1, settings.TTS_REACHABILITY_PROBE_TIMEOUT / 10)))


async def run_all(settings: Settings) -> list[CheckResult]:
    """Run every reachability check (advisory).

    Logs each result and a summary warning if any dependency is down, but never
    raises -- the worker enters its poll loop regardless so an unconfigured or
    temporarily-unreachable dependency (Firecrawl/LLM/TTS) doesn't block the
    whole stack from starting. A job that hits a down dependency fails that
    stage with a clear error, and /health/ready reports the live status.
    """

    results: list[CheckResult] = [
        await check_firecrawl(settings),
        await check_llm(settings),
        await check_tts(settings),
    ]
    failed = [r for r in results if not r.ok]
    for result in results:
        logger.info(
            "Reachability check",
            # Use `phase` instead of `stage` so reachability events don't
            # share a Loki label with the pipeline-stage contextvar
            # (extract/cleanup/corrections/...). Operators querying by stage
            # see only pipeline events.
            extra={
                "event": "reachability_check",
                "phase": "startup",
                "check": result.name,
                "ok": result.ok,
                "detail": result.detail,
            },
        )
    if failed:
        names = ", ".join(r.name for r in failed)
        logger.warning(
            "Reachability: some dependencies are down; starting anyway "
            "(jobs needing them will fail that stage until they recover)",
            extra={"event": "reachability_degraded", "phase": "startup", "down": names},
        )
    return results
