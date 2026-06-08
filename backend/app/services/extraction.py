"""Firecrawl extraction client.

Wraps the self-hosted Firecrawl ``/v1/scrape`` endpoint with tenacity retries on
transient failures and a minimum-length guard. Other stages of the pipeline see
a clean ``ExtractionResult`` or a typed exception.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
import trafilatura
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings
from app.services.source_fallbacks import SourceFallback, candidate_attempts, match

logger = logging.getLogger("app.services.extraction")


@dataclass(frozen=True)
class ExtractionResult:
    """Parsed Firecrawl response. ``markdown`` is the cleaned-page body; metadata
    holds anything later phases may want (title, og:image, author)."""

    markdown: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ExtractionError(Exception):
    """Base class so callers can do a single except for any extraction failure."""


class ExtractionTransientError(ExtractionError):
    """5xx, connection refused, timeout. Tenacity retries these."""


class ExtractionPermanentError(ExtractionError):
    """4xx, malformed response, or any other non-retryable failure."""


class ExtractionTooShortError(ExtractionPermanentError):
    """No scrape (direct or fallback) cleared the minimum length.

    The floor is ``MIN_EXTRACTION_CHARS`` by default, or a source-specific
    ``min_chars`` when a ``source_fallbacks`` rule matched the host.
    """


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
        solver_tried = False

        # FlareSolverr: fetch the URL through the operator's solver (a real browser).
        # Three triggers, all needing FLARESOLVERR_URL: (1) CHALLENGE -- the scrape
        # looks like a Cloudflare/bot-challenge page. (2) HARD-BLOCK -- the scrape came
        # back near-empty (below MIN_EXTRACTION_CHARS), i.e. the site served almost
        # nothing (a 403/IP block like NYT) where a headers-only fetch can't help and
        # the solver's residential browser can. (3) HOST-RULE -- the matched host
        # explicitly selected the "flaresolverr" strategy. All bounded: a plain teaser
        # (real text >= MIN_EXTRACTION_CHARS) never pays for a browser solve.
        is_challenge = _looks_like_challenge(result)
        near_empty = len(result.markdown) < settings.MIN_EXTRACTION_CHARS
        if settings.FLARESOLVERR_URL.strip() and (is_challenge or flaresolverr_rule or near_empty):
            solver_tried = True
            trigger = (
                "host-rule" if flaresolverr_rule else "challenge" if is_challenge else "hard-block"
            )
            logger.info(
                "Routing below-floor scrape through FlareSolverr",
                extra={
                    "event": "extraction_flaresolverr_route",
                    "trigger": trigger,
                    "host": (urlsplit(url).hostname or "").lower(),
                    "rule": rule.name if rule is not None else None,
                    "primary_chars": len(result.markdown),
                },
            )
            alt = await _fetch_via_flaresolverr(url, settings)
            label = f"{trigger}#flaresolverr"
            if alt is not None:
                best_chars = max(best_chars, len(alt.markdown))
                # The solver returns the full page (no teaser to filter), so accept it
                # against the hard MIN_EXTRACTION_CHARS, not a rule's higher teaser floor
                # (a near-empty scrape can route here even under a googlebot rule).
                if len(alt.markdown) >= settings.MIN_EXTRACTION_CHARS:
                    _log_fallback_used(label, result.markdown, alt.markdown)
                    return alt
                _log_fallback_short(label, len(alt.markdown), settings.MIN_EXTRACTION_CHARS)

        # Per-host paywall strategy. A matched rule supplies the bypass attempts
        # ("googlebot" re-scrapes the same url with crawler headers; "freedium" /
        # "custom" rewrite the url). Logged either way so the path is traceable:
        # which strategy ran, or that no rule matched, plus when an attempt ran but
        # still came back short (previously silent).
        if rule is None:
            logger.info(
                "Direct scrape below floor; no per-host rule and no global default proxy",
                extra={
                    "event": "extraction_no_fallback_rule",
                    "host": (urlsplit(url).hostname or "").lower(),
                    "primary_chars": len(result.markdown),
                    "floor": floor,
                },
            )
        elif not flaresolverr_rule:
            # flaresolverr rules were already handled above via the solver; their
            # candidate_attempts is empty, so skip the misleading "attempting bypass".
            logger.info(
                "Direct scrape below floor; attempting bypass",
                extra={
                    "event": "extraction_fallback_start",
                    "rule": rule.name,
                    "strategy": rule.proxy,
                    "primary_chars": len(result.markdown),
                    "floor": floor,
                },
            )
            for label, candidate, target_headers in candidate_attempts(rule, url):
                try:
                    alt = await _scrape(client, candidate, settings, headers=target_headers or None)
                except ExtractionError as exc:
                    logger.warning(
                        "Extraction fallback attempt failed",
                        extra={
                            "event": "extraction_fallback_failed",
                            "fallback": label,
                            "error": str(exc),
                        },
                    )
                    continue
                best_chars = max(best_chars, len(alt.markdown))
                if len(alt.markdown) >= floor:
                    _log_fallback_used(label, result.markdown, alt.markdown)
                    return alt
                _log_fallback_short(label, len(alt.markdown), floor)

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


# Substrings that mark a Cloudflare / bot-challenge interstitial rather than a
# real article. Matched case-insensitively against a below-floor scrape's
# markdown + title; chosen to be specific to challenge pages so a real (short)
# article rarely collides. This is the only thing that triggers a FlareSolverr
# solve, so a plain paywall teaser never pays for a browser.
_CHALLENGE_MARKERS = (
    "just a moment...",
    "attention required! | cloudflare",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "cf-browser-verification",
    "challenge-platform",
    "cloudflare ray id",
    "verify you are human",
    "verify you are a human",
    "ddos-guard",
    "pardon our interruption",
    "access to this page has been denied",
    "sorry, you have been blocked",
)


def _looks_like_challenge(result: ExtractionResult) -> bool:
    """True when a below-floor scrape looks like a Cloudflare/bot-challenge page
    (the case FlareSolverr is meant to solve) rather than a real short article."""

    haystack = f"{result.markdown} {result.metadata.get('title', '')}".lower()
    return any(marker in haystack for marker in _CHALLENGE_MARKERS)


async def _fetch_via_flaresolverr(url: str, settings: Settings) -> ExtractionResult | None:
    """Solve and fetch ``url`` through FlareSolverr, returning article markdown.

    FlareSolverr runs a real browser to clear a Cloudflare/JS challenge and hands
    back the solved HTML; trafilatura pulls the article body out of it. Any failure
    (unset URL, solver error, non-200 target, empty extraction) returns ``None`` so
    the caller falls through to the too-short error -- this never raises, so a flaky
    solver can't turn a clean teaser-fail into a stack trace. Uses its own client
    (no Firecrawl bearer/timeout) and matches the public FlareSolverr ``/v1`` shape.
    """

    endpoint = settings.FLARESOLVERR_URL.strip().rstrip("/")
    if not endpoint:
        return None
    if not endpoint.endswith("/v1"):
        endpoint = f"{endpoint}/v1"
    # The read budget must exceed the solver's own maxTimeout so we don't cancel
    # it mid-solve; +30s covers browser spin-up plus our network hop. Connect is
    # kept short so an unreachable solver fails fast instead of stalling the
    # worker for the whole read budget.
    read_timeout = settings.FLARESOLVERR_MAX_TIMEOUT_MS / 1000 + 30
    timeout = httpx.Timeout(read_timeout, connect=10.0)
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": settings.FLARESOLVERR_MAX_TIMEOUT_MS,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(endpoint, json=payload)
    except httpx.HTTPError as exc:
        logger.warning(
            "FlareSolverr request failed",
            extra={"event": "flaresolverr_unreachable", "error": str(exc)},
        )
        return None

    try:
        body = response.json()
    except ValueError:
        logger.warning("FlareSolverr returned non-JSON", extra={"event": "flaresolverr_bad_response"})
        return None
    if not isinstance(body, dict) or body.get("status") != "ok":
        message = body.get("message") if isinstance(body, dict) else None
        logger.warning(
            "FlareSolverr did not solve the challenge",
            extra={"event": "flaresolverr_error", "message": message},
        )
        return None

    # A truthy non-dict solution (list/str from a malformed or hostile solver)
    # would make solution.get(...) raise AttributeError -- guard it like `body`.
    solution = body.get("solution")
    if not isinstance(solution, dict):
        logger.warning("FlareSolverr returned no solution", extra={"event": "flaresolverr_no_solution"})
        return None
    if solution.get("status") != 200:
        logger.warning(
            "FlareSolverr fetched a non-200 page",
            extra={"event": "flaresolverr_target_status", "status": solution.get("status")},
        )
        return None
    html = solution.get("response")
    if not isinstance(html, str):
        logger.warning("FlareSolverr response was not HTML text", extra={"event": "flaresolverr_bad_html"})
        return None

    markdown, metadata = _html_to_markdown(html)
    if not markdown:
        logger.warning(
            "FlareSolverr HTML yielded no article text",
            extra={"event": "flaresolverr_empty_extract"},
        )
        return None
    return ExtractionResult(markdown=markdown, metadata=metadata)


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


# Cap the solved HTML before lxml builds a DOM (several times the source size in
# memory) so a pathologically large, attacker-controlled page can't OOM the
# worker. No real article is anywhere near this; the artwork path caps downloads
# for the same reason (ARTWORK_MAX_DOWNLOAD_BYTES).
_MAX_SOLVED_HTML_CHARS = 8_000_000


def _html_to_markdown(html: str) -> tuple[str, dict[str, Any]]:
    """Extract the main article body from raw HTML as markdown, plus best-effort
    title/author/og:image metadata mapped into the same keys the finalize and
    artwork stages already read from Firecrawl. Returns ``("", {})`` when there is
    no extractable article. Never raises -- the HTML is attacker-controlled."""

    if not html.strip():
        return "", {}
    if len(html) > _MAX_SOLVED_HTML_CHARS:
        logger.warning(
            "FlareSolverr HTML exceeds the size cap; skipping",
            extra={"event": "flaresolverr_html_oversize", "chars": len(html)},
        )
        return "", {}
    try:
        markdown = (
            trafilatura.extract(
                html, output_format="markdown", include_comments=False, include_tables=True
            )
            or ""
        )
        meta = trafilatura.extract_metadata(html)
    except Exception:  # adversarial HTML; never fail extraction on a parse error
        logger.warning(
            "trafilatura could not parse the FlareSolverr HTML",
            extra={"event": "flaresolverr_parse_error"},
        )
        return "", {}
    metadata: dict[str, Any] = {}
    if meta is not None:
        if getattr(meta, "title", None):
            metadata["title"] = meta.title
        if getattr(meta, "author", None):
            metadata["author"] = meta.author
        if getattr(meta, "image", None):
            metadata["ogImage"] = meta.image  # the key artwork._extract_og_image reads first
    return markdown.strip(), metadata


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
