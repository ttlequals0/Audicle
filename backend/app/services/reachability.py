"""Startup reachability checks.

Each external dependency the pipeline needs is probed before the worker begins
processing. A failure here is fatal: the process exits non-zero so the
container restart loop surfaces the problem instead of every job failing in
the same way mid-stage.

Phase 2 only checks Firecrawl. The LLM probe lands in Phase 3 and the TTS
wrapper probe in Phase 4 -- see the table in build-plan.md "Startup
Reachability Checks".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import Settings

logger = logging.getLogger("app.services.reachability")


class ReachabilityError(RuntimeError):
    """Raised when a required external dependency is unreachable at startup."""


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


async def run_all(settings: Settings) -> list[CheckResult]:
    """Run every Phase-appropriate check.

    Logs each result and raises ``ReachabilityError`` if any check failed. The
    worker bootstrap calls this; the FastAPI lifespan doesn't (so reviewers
    using the API for /health while a dependency is down still get a useful
    503 instead of a refusing-to-start container).
    """

    results: list[CheckResult] = [await check_firecrawl(settings)]
    failed = [r for r in results if not r.ok]
    for result in results:
        logger.info(
            "Reachability check",
            extra={
                "event": "reachability_check",
                "stage": "startup",
                "check": result.name,
                "ok": result.ok,
                "detail": result.detail,
            },
        )
    if failed:
        names = ", ".join(r.name for r in failed)
        raise ReachabilityError(f"reachability checks failed: {names}")
    return results
