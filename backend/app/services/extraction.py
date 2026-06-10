"""Firecrawl extraction client.

Wraps the self-hosted Firecrawl ``/v1/scrape`` endpoint with tenacity retries on
transient failures and a minimum-length guard. Other stages of the pipeline see
a clean ``ExtractionResult`` or a typed exception.
"""

from __future__ import annotations

import json
import logging
import re
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
from app.services import archive, flaresolverr, ssrf

# Re-exported so existing ``extraction.ExtractionResult`` / ``extraction.ExtractionTooShortError``
# references (pipeline, tests) keep working now that the types live in extraction_types, which
# the FlareSolverr engine also imports without a circular dependency.
from app.services.extraction_types import (
    ExtractionError,
    ExtractionPermanentError,
    ExtractionResult,
    ExtractionTooShortError,
    ExtractionTransientError,
)
from app.services.source_fallbacks import Attempt, SourceFallback, candidate_attempts, match

logger = logging.getLogger("app.services.extraction")


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

    # SSRF chokepoint for every extraction path (pipeline worker + the
    # /source-fallbacks/test endpoint): refuse a URL whose host resolves to a
    # non-public address before any fetch. The submit endpoint blocks this at
    # enqueue too; this is the defense-in-depth backstop. The resolved IP is not
    # surfaced in the message (the test endpoint echoes str(exc)). Only a
    # confirmed non-public address is a permanent block -- a resolution failure
    # (transient DNS, NXDOMAIN) falls through to the normal Firecrawl path, which
    # has its own retry/timeout handling, so a DNS blip isn't a permanent failure.
    try:
        await ssrf.assert_url_public(url)
    except ssrf.BlockedHostError as exc:
        if exc.blocked:
            raise ExtractionPermanentError(
                "The article URL resolves to a non-public address and was blocked."
            ) from exc

    # A matched rule (per-host override or the global-default catch-all) raises the
    # bar (teasers clear the global floor) and supplies the bypass attempts.
    # Disabling the feature reverts to plain behavior.
    rule = match(url, registry) if settings.EXTRACTION_FALLBACKS_ENABLED else None
    # A flaresolverr rule keeps the rule's teaser floor like every strategy, so a
    # teaser paywall (real text but below the floor) drops below it and routes to the
    # solver instead of being silently returned. The solver's full-page result is then
    # accepted against the hard MIN_EXTRACTION_CHARS in the loop.
    floor = settings.MIN_EXTRACTION_CHARS if rule is None else rule.min_chars
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
        # Only a flagged host pays for rawHtml + the JSON-LD teaser check; a free article
        # with no rule keeps the plain scrape, so the common path stays cheap.
        result = await _scrape(client, url, settings, detect_teaser=rule is not None)
        if _effective_chars(result, rule, floor) >= floor:
            return result
        # Best result seen across direct + every bypass, and whether the browser
        # solver was attempted -- together they classify the failure message.
        best_chars = _effective_chars(result, rule, floor)

        # Build one ordered bypass plan. The rule's own strategy supplies its attempts
        # (googlebot/freedium/custom/flaresolverr); on top, FlareSolverr auto-escalates
        # for ANY host when the scrape looks like a Cloudflare challenge or is near-empty
        # (a hard 403/IP block) -- unless the plan already routes to the solver. The auto
        # attempt is prepended so the browser solve runs first. Every attempt (browser or
        # Firecrawl re-scrape) runs through one dispatcher, so a plain teaser (real text)
        # never pays for a browser solve.
        attempts: list[Attempt] = candidate_attempts(rule, url) if rule is not None else []
        solver_configured = bool(settings.FLARESOLVERR_URL.strip())
        near_empty = (
            _effective_chars(result, rule, settings.MIN_EXTRACTION_CHARS)
            < settings.MIN_EXTRACTION_CHARS
        )
        if solver_configured and not any(a.engine == "flaresolverr" for a in attempts):
            is_challenge = flaresolverr.looks_like_challenge(result)
            if is_challenge or near_empty:
                trigger = "challenge" if is_challenge else "hard-block"
                attempts.insert(0, Attempt(f"{trigger}#flaresolverr", "flaresolverr", url))
        # Last resort for any near-empty scrape: a Wayback capture (no cookies, no bot
        # wall). Appended last so live strategies and the solver run first; archive.today
        # (via the solver) stays opt-in behind an explicit per-host archive rule.
        if (
            settings.ARCHIVE_FALLBACK_ENABLED
            and near_empty
            and not any(a.engine == "archive" for a in attempts)
        ):
            attempts.append(Attempt("auto#archive", "archive", url))

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
        solver_sent_cookies = False
        fallback_start_logged = False
        for attempt in attempts:
            # A host-rule attempt is held to the rule's teaser floor (an archived or solved
            # teaser is still a teaser); an auto-escalation attempt accepts the full page
            # against the hard MIN.
            accept_floor = floor if attempt.is_host_rule else settings.MIN_EXTRACTION_CHARS
            if attempt.engine == "flaresolverr":
                if not solver_configured:  # a flaresolverr rule but no solver URL set
                    continue
                solver_tried = True
                solver_sent_cookies = solver_sent_cookies or bool(attempt.cookies)
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
            elif attempt.engine == "archive":
                logger.info(
                    "Routing below-floor scrape through a web archive",
                    extra={
                        "event": "extraction_archive_route",
                        "host": host,
                        "rule": rule.name if rule is not None else None,
                        "primary_chars": len(result.markdown),
                    },
                )
                # A host-rule grab also tries archive.today (via the solver); the auto
                # last-resort is Wayback-only.
                alt = await archive.fetch(
                    attempt.url, settings, include_archive_today=attempt.is_host_rule
                )
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
                    alt = await _scrape(
                        client,
                        attempt.url,
                        settings,
                        headers=attempt.headers or None,
                        detect_teaser=True,
                    )
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
            if alt is None:
                continue
            alt_chars = _effective_chars(alt, rule, accept_floor)
            best_chars = max(best_chars, alt_chars)
            if alt_chars >= accept_floor:
                _log_fallback_used(attempt.label, result.markdown, alt.markdown)
                return alt
            _log_fallback_short(attempt.label, alt_chars, accept_floor)

    # Only claim "your cookies look expired" when a solver attempt actually carried
    # cookies -- an auto-escalation solver runs without them, so the rule merely having
    # cookies isn't enough.
    raise ExtractionTooShortError(
        _too_short_message(best_chars, solver_tried, settings, solver_sent_cookies)
    )


def _too_short_message(
    best_chars: int, solver_tried: bool, settings: Settings, cookies_present: bool = False
) -> str:
    """Short, plain failure reason for the Home UI: what kind of block, and the fix.

    Keyed on whether the browser solver was tried, not just the char count: if the
    solver fired and still came up short, an IP/UA swap won't help. When it ran with the
    operator's cookies and still got a teaser, the cookies are the likely culprit
    (expired/invalid); without cookies the site needs a login. Otherwise a near-empty
    scrape (below ``MIN_EXTRACTION_CHARS``) is a hard 403/IP block whose fix is
    FlareSolverr; anything above that is a metered teaser to route through a per-host
    bypass.
    """

    if solver_tried:
        if cookies_present:
            return (
                "Still paywalled: the browser bypass used your saved cookies but got only a "
                "teaser. They're probably expired -- re-paste them in Settings."
            )
        return (
            "Still paywalled: the browser bypass got only a teaser. This site needs a login "
            "-- add its subscriber cookies in Settings."
        )
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


_LD_SCRIPT_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def _iter_ld_nodes(data: Any) -> Any:
    """Yield dict nodes from a parsed JSON-LD blob, flattening the ``@graph`` wrapper
    and top-level lists so an ``articleBody`` is found wherever the page puts it."""

    if isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                if isinstance(item, dict):
                    yield item
        yield data
    elif isinstance(data, list):
        for item in data:
            yield from _iter_ld_nodes(item)


def _article_body_chars(raw_html: str) -> int | None:
    """Length of the longest JSON-LD ``articleBody`` the page declares, or None.

    This is the publisher's own article text, so it ignores the related-article and
    navigation chrome that can pad a scraped teaser past the floor. Never raises --
    the HTML and its embedded JSON are attacker-controlled."""

    if not raw_html:
        return None
    best: int | None = None
    for script in _LD_SCRIPT_RE.finditer(raw_html):
        try:
            data = json.loads(script.group(1).strip())
        except ValueError:
            continue
        for node in _iter_ld_nodes(data):
            body = node.get("articleBody")
            if isinstance(body, str):
                best = max(best or 0, len(body.strip()))
    return best


def _effective_chars(result: ExtractionResult, rule: SourceFallback | None, floor: int) -> int:
    """Body length for the floor decision. For an operator-flagged host (a matched
    rule), when the page's own JSON-LD says the article body is below the floor but the
    scraped markdown clears it, the surplus is related-article/nav chrome -- trust the
    declared body length so the teaser routes to a bypass. Unflagged hosts and pages
    with no declared body keep the plain scraped length, so free articles are untouched."""

    scraped = len(result.markdown)
    declared = result.article_chars
    if rule is not None and declared is not None and declared < floor <= scraped:
        return declared
    return scraped


def _build_payload(
    url: str,
    settings: Settings,
    extra_headers: dict[str, str] | None = None,
    detect_teaser: bool = False,
) -> dict[str, Any]:
    # rawHtml rides along only when we'll read the page's JSON-LD articleBody (a flagged
    # host) -- it roughly doubles the response, so a free article doesn't pay for it.
    formats = ["markdown", "rawHtml"] if detect_teaser else ["markdown"]
    payload: dict[str, Any] = {
        "url": url,
        "formats": formats,
        "onlyMainContent": settings.FIRECRAWL_ONLY_MAIN_CONTENT,
        "removeBase64Images": settings.FIRECRAWL_REMOVE_BASE64_IMAGES,
        # Force a fresh scrape every time: Firecrawl caches by URL, so without this a
        # reprocess (or a re-submit after changing a bypass rule/cookies) would get the
        # stale cached result and ignore the new config.
        "maxAge": 0,
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
    detect_teaser: bool = False,
) -> ExtractionResult:
    """One scrape (with retries) -> ExtractionResult. Length is validated by the caller.
    ``detect_teaser`` requests rawHtml and records the JSON-LD ``articleBody`` length so a
    chrome-padded teaser can be told from a real article; off for unflagged hosts."""

    endpoint = f"{settings.FIRECRAWL_URL.rstrip('/')}/v1/scrape"
    payload = _build_payload(url, settings, headers, detect_teaser)
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
    raw_html = data.get("rawHtml")
    article_chars = _article_body_chars(raw_html) if isinstance(raw_html, str) else None
    return ExtractionResult(markdown=markdown, metadata=metadata, article_chars=article_chars)


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
