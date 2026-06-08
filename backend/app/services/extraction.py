"""Firecrawl extraction client.

Wraps the self-hosted Firecrawl ``/v1/scrape`` endpoint with tenacity retries on
transient failures and a minimum-length guard. Other stages of the pipeline see
a clean ``ExtractionResult`` or a typed exception.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings
from app.services import flaresolverr
from app.services.source_fallbacks import Attempt, SourceFallback, candidate_attempts, match

logger = logging.getLogger("app.services.extraction")


# Re-exported from extraction_types so existing ``extraction.ExtractionResult`` /
# ``extraction.ExtractionTooShortError`` references (pipeline, tests) keep working
# while the FlareSolverr engine imports the same types without a circular dependency.
from app.services.extraction_types import (  # noqa: E402
    ExtractionError,
    ExtractionPermanentError,
    ExtractionResult,
    ExtractionTooShortError,
    ExtractionTransientError,
)


async def extract(
    url: str,
    settings: Settings,
    registry: tuple[SourceFallback, ...] | None = None,
) -> ExtractionResult:
    """Scrape ``url`` via Firecrawl and validate the result.

    A direct scrape that comes back below its floor is retried with a bypass
    strategy. ``registry`` is the effective rule set: per-host rules (operator config
    over built-ins) plus, when the operator set a global default proxy, a
    lowest-priority catch-all so the default applies to *any* host whose scrape is
    near-empty (below ``MIN_EXTRACTION_CHARS``). A per-host rule overrides the
    catch-all, winning on host match and keeping its higher teaser floor (a known
    paywall serves a teaser that clears the global floor but is useless to narrate).
    Separately, and for any host, a below-floor scrape that looks like a Cloudflare/
    bot-challenge page is automatically re-fetched through FlareSolverr (when
    ``FLARESOLVERR_URL`` is set) -- gated on challenge detection so a plain teaser
    never triggers a browser solve. ``None`` uses the built-ins only.

    Raises:
        ExtractionTransientError: every retry exhausted on a retryable failure.
        ExtractionPermanentError: 4xx, malformed JSON, or other non-retryable.
        ExtractionTooShortError: no candidate cleared the minimum length.
    """

    # A matched rule (per-host override or the global-default catch-all) raises the
    # bar (teasers clear the global floor) and supplies the bypass attempts.
    # Disabling the feature reverts to plain behavior.
    rule = match(url, registry) if settings.EXTRACTION_FALLBACKS_ENABLED else None
    # FlareSolverr fetches the full page via a real browser (no teaser to filter), so a
    # flaresolverr rule uses the hard MIN_EXTRACTION_CHARS floor rather than the higher
    # teaser min_chars the googlebot/freedium strategies need to spot a stub.
    flaresolverr_rule = rule is not None and rule.proxy == "flaresolverr"
    floor = (
        settings.MIN_EXTRACTION_CHARS if (rule is None or flaresolverr_rule) else rule.min_chars
    )
    host = (urlsplit(url).hostname or "").lower()

    timeout = httpx.Timeout(settings.FIRECRAWL_TIMEOUT_SECONDS)
    # Bearer auth only when a key is configured; an open self-hosted Firecrawl
    # sends no Authorization header.
    headers = (
        {"Authorization": f"Bearer {settings.FIRECRAWL_API_KEY}"}
        if settings.FIRECRAWL_API_KEY
        else None
    )

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        result = await _scrape(client, url, settings)
        if len(result.markdown) >= floor:
            return result
        # Best result seen across direct + every bypass, and whether the browser
        # solver was attempted -- together they classify the failure message.
        best_chars = len(result.markdown)

        # Build one ordered bypass plan. FlareSolverr auto-escalates for ANY host when
        # the scrape looks like a Cloudflare challenge or is near-empty (a hard 403/IP
        # block) -- unless the rule already routes to the solver -- then the rule's own
        # strategy attempts. Every attempt (browser or Firecrawl re-scrape) runs through
        # one dispatcher, so a plain teaser (real text) never pays for a browser solve.
        attempts: list[Attempt] = []
        solver_configured = bool(settings.FLARESOLVERR_URL.strip())
        near_empty = len(result.markdown) < settings.MIN_EXTRACTION_CHARS
        if solver_configured and not flaresolverr_rule and (
            flaresolverr.looks_like_challenge(result) or near_empty
        ):
            trigger = "challenge" if flaresolverr.looks_like_challenge(result) else "hard-block"
            attempts.append(Attempt(f"{trigger}#flaresolverr", "flaresolverr", url))
        if rule is not None:
            attempts += candidate_attempts(rule, url)

        if rule is None:
            logger.info(
                "Direct scrape below floor; no per-host rule and no global default proxy",
                extra={
                    "event": "extraction_no_fallback_rule",
                    "host": host,
                    "primary_chars": len(result.markdown),
                    "floor": floor,
                },
            )

        solver_tried = False
        fallback_start_logged = False
        for attempt in attempts:
            if attempt.engine == "flaresolverr":
                if not solver_configured:  # a flaresolverr rule but no solver URL set
                    continue
                solver_tried = True
                logger.info(
                    "Routing below-floor scrape through FlareSolverr",
                    extra={
                        "event": "extraction_flaresolverr_route",
                        "trigger": attempt.label.split("#")[0],
                        "host": host,
                        "rule": rule.name if rule is not None else None,
                        "primary_chars": len(result.markdown),
                    },
                )
                alt = await flaresolverr.fetch(attempt.url, settings, attempt.cookies)
                # The solver returns the full page (no teaser to filter), so accept it
                # against the hard MIN_EXTRACTION_CHARS, not a rule's higher teaser floor.
                accept_floor = settings.MIN_EXTRACTION_CHARS
            else:  # firecrawl re-scrape (googlebot/freedium/custom)
                if not fallback_start_logged:
                    logger.info(
                        "Direct scrape below floor; attempting bypass",
                        extra={
                            "event": "extraction_fallback_start",
                            "rule": rule.name if rule is not None else None,
                            "strategy": rule.proxy if rule is not None else None,
                            "primary_chars": len(result.markdown),
                            "floor": floor,
                        },
                    )
                    fallback_start_logged = True
                try:
                    alt = await _scrape(client, attempt.url, settings, headers=attempt.headers or None)
                except ExtractionError as exc:
                    logger.warning(
                        "Extraction fallback attempt failed",
                        extra={
                            "event": "extraction_fallback_failed",
                            "fallback": attempt.label,
                            "error": str(exc),
                        },
                    )
                    continue
                accept_floor = floor
            if alt is None:
                continue
            best_chars = max(best_chars, len(alt.markdown))
            if len(alt.markdown) >= accept_floor:
                _log_fallback_used(attempt.label, result.markdown, alt.markdown)
                return alt
            _log_fallback_short(attempt.label, len(alt.markdown), accept_floor)

    raise ExtractionTooShortError(_too_short_message(best_chars, solver_tried, settings))


def _too_short_message(best_chars: int, solver_tried: bool, settings: Settings) -> str:
    """Short, plain failure reason for the Home UI: what kind of block, and the fix.

    Keyed on whether the browser solver was tried, not just the char count: if the
    solver fired and still came up short, an IP/UA swap won't help. Otherwise a
    near-empty scrape (below ``MIN_EXTRACTION_CHARS``) is a hard 403/IP block whose
    fix is FlareSolverr; anything above that is a metered teaser the operator can
    route through a per-host bypass.
    """

    if solver_tried:
        return "Blocked: the browser bypass couldn't get the article. The site likely needs a login."
    if best_chars < settings.MIN_EXTRACTION_CHARS:
        return (
            "Hard block: the site sent almost nothing. Set FLARESOLVERR_URL in "
            "Connections to retry in a browser."
        )
    return (
        f"Short teaser, {best_chars} chars. Looks like a paywall; add a bypass for "
        "this host in Settings."
    )


def _log_fallback_used(label: str, primary_markdown: str, alt_markdown: str) -> None:
    """Shared success log for both fallback paths so their telemetry can't drift."""

    logger.info(
        "Extraction fallback succeeded",
        extra={
            "event": "extraction_fallback_used",
            "fallback": label,
            "primary_chars": len(primary_markdown),
            "markdown_chars": len(alt_markdown),
        },
    )


def _log_fallback_short(label: str, alt_chars: int, floor: int) -> None:
    """A fallback attempt ran but came back below the floor. Logged so a bypass
    that runs yet doesn't help (e.g. a hard subscription paywall serving the same
    teaser to the Googlebot fetch) is visible in logs instead of silent."""

    logger.info(
        "Extraction fallback attempt below floor",
        extra={
            "event": "extraction_fallback_short",
            "fallback": label,
            "markdown_chars": alt_chars,
            "floor": floor,
        },
    )


def _build_payload(
    url: str, settings: Settings, extra_headers: dict[str, str] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": settings.FIRECRAWL_ONLY_MAIN_CONTENT,
        "removeBase64Images": settings.FIRECRAWL_REMOVE_BASE64_IMAGES,
    }
    if settings.firecrawl_exclude_tags:
        payload["excludeTags"] = settings.firecrawl_exclude_tags
    # Firecrawl forwards these to the target site -- the Googlebot bypass sends a
    # crawler User-Agent + X-Forwarded-For here.
    if extra_headers:
        payload["headers"] = extra_headers
    return payload


async def _scrape(
    client: httpx.AsyncClient,
    url: str,
    settings: Settings,
    headers: dict[str, str] | None = None,
) -> ExtractionResult:
    """One scrape (with retries) -> ExtractionResult. Length is validated by the caller."""

    endpoint = f"{settings.FIRECRAWL_URL.rstrip('/')}/v1/scrape"
    payload = _build_payload(url, settings, headers)
    try:
        response = await _post_with_retry(client, endpoint, payload, settings)
    except RetryError as exc:
        inner = exc.last_attempt.exception()
        if isinstance(inner, ExtractionError):
            raise inner from exc
        raise ExtractionTransientError(f"Firecrawl retries exhausted: {inner}") from exc

    body = _parse_response(response, url)
    data = body.get("data") or {}
    if not isinstance(data, dict):
        raise ExtractionPermanentError(
            f"Firecrawl returned non-object `data` for {url}: {type(data).__name__}"
        )
    markdown = data.get("markdown") or ""
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    return ExtractionResult(markdown=markdown, metadata=metadata)


async def _post_with_retry(
    client: httpx.AsyncClient,
    endpoint: str,
    payload: dict[str, Any],
    settings: Settings,
) -> httpx.Response:
    retrying = AsyncRetrying(
        stop=stop_after_attempt(settings.FIRECRAWL_RETRY_COUNT),
        wait=wait_exponential(
            multiplier=settings.FIRECRAWL_BACKOFF_BASE_SECONDS,
            min=settings.FIRECRAWL_BACKOFF_BASE_SECONDS,
        ),
        retry=retry_if_exception_type(ExtractionTransientError),
        reraise=False,
    )
    async for attempt in retrying:
        with attempt:
            try:
                response = await client.post(endpoint, json=payload)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise ExtractionTransientError(f"Firecrawl unreachable: {exc}") from exc
            _raise_for_status(response)
        if attempt.retry_state.outcome and not attempt.retry_state.outcome.failed:
            return response
    # AsyncRetrying with reraise=False either returns from inside the `with`
    # block (success) or raises RetryError (caught by the caller); unreachable
    # in practice but the type checker wants a terminal return/raise.
    raise ExtractionTransientError("Firecrawl retry loop exited without a response")


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_server_error:
        raise ExtractionTransientError(
            f"Firecrawl returned {response.status_code}: {response.text[:200]}"
        )
    if response.is_client_error:
        raise ExtractionPermanentError(
            f"Firecrawl rejected request ({response.status_code}): {response.text[:200]}"
        )


def _parse_response(response: httpx.Response, url: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise ExtractionPermanentError(
            f"Firecrawl returned non-JSON body for {url}: {exc}"
        ) from exc
    if not isinstance(body, dict):
        raise ExtractionPermanentError(
            f"Firecrawl returned non-object JSON for {url}: {type(body).__name__}"
        )
    if not body.get("success", False):
        raise ExtractionPermanentError(
            f"Firecrawl returned success=false for {url}: {body.get('error', '<no error>')}"
        )
    return body
