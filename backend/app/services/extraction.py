"""Article extraction orchestrator.

Runs the primary engine -- the in-process ``direct`` fetcher (default) or the
self-hosted ``firecrawl`` client, selected by ``EXTRACTION_ENGINE`` -- then validates
the result against a minimum-length floor and, when it falls short, walks an
engine-agnostic fallback cascade (per-host bypass rules, FlareSolverr, web archive,
Arc XP). Other stages of the pipeline see a clean ``ExtractionResult`` or a typed
exception. The Firecrawl client and its ``/v1/scrape`` retry logic live below in
``_scrape``; the direct engine lives in ``direct_fetch``.
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
from app.services import (
    arc_extractor,
    archive,
    direct_fetch,
    flaresolverr,
    jsonld,
    reader,
    render,
    ssrf,
)

# Re-exported so existing ``extraction.ExtractionResult`` / ``extraction.ExtractionTooShortError``
# references (pipeline, tests) keep working now that the types live in extraction_types, which
# the FlareSolverr engine also imports without a circular dependency.
from app.services.extraction_types import (
    BLOCKED_STATUS_CODES,
    ExtractionBlockedError,
    ExtractionError,
    ExtractionPermanentError,
    ExtractionResult,
    ExtractionTooShortError,
    ExtractionTransientError,
    scan_markers,
)
from app.services.source_fallbacks import Attempt, SourceFallback, candidate_attempts, match

logger = logging.getLogger("app.services.extraction")


async def extract(
    url: str,
    settings: Settings,
    registry: tuple[SourceFallback, ...] | None = None,
) -> ExtractionResult:
    """Fetch ``url`` with the configured engine and validate the result.

    A primary fetch that comes back below its floor is retried with a bypass
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
    # enqueue too; this is the defense-in-depth backstop. The resolved IP is
    # deliberately not surfaced in the raised message. Only a
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
    # A render rule is a post-cascade strategy (it emits no loop attempt), so it uses the
    # global floor, NOT its own min_chars -- otherwise a blocked/empty primary would
    # "clear" a render rule's min_chars and skip the cascade and the render rescue.
    floor = (
        settings.MIN_EXTRACTION_CHARS
        if rule is None or rule.proxy == "render"
        else rule.min_chars
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
        # A flagged host pays for rawHtml + the JSON-LD teaser check; Arc detection also
        # needs the page HTML, so request it when the Arc extractor is enabled.
        detect_teaser = rule is not None or settings.EXTRACTION_ARC_ENABLED
        try:
            if settings.EXTRACTION_ENGINE == "direct":
                result = await direct_fetch.fetch(url, settings, detect_teaser=detect_teaser)
            else:
                result = await _scrape(client, url, settings, detect_teaser=detect_teaser)
        except ExtractionBlockedError as exc:
            # 403/429: the primary engine was blocked (IP/WAF/rate limit). Treat it as a
            # near-empty scrape so the bypass cascade below (FlareSolverr from a different
            # IP, then a Wayback capture) gets a chance, instead of dead-ending the job.
            # If no bypass clears the floor, the cascade raises its own blocked message.
            logger.info(
                "Primary engine blocked; routing to fallback cascade",
                extra={"event": "extraction_blocked_primary", "host": host, "detail": str(exc)},
            )
            result = ExtractionResult(markdown="")
        # Arc XP / Fusion: the visible scrape may be a teaser while the full body sits in
        # the page's content_elements JSON. When Arc finds a longer body, prefer it -- then
        # the floor check + fallbacks below run against the real article.
        if settings.EXTRACTION_ARC_ENABLED and result.raw_html:
            arc_md = arc_extractor.extract_body(result.raw_html)
            if (
                arc_md
                and len(arc_md) >= settings.MIN_EXTRACTION_CHARS
                and len(arc_md) > len(result.markdown)
            ):
                logger.info(
                    "Arc XP static body extracted",
                    extra={
                        "event": "extraction_arc_hit",
                        "host": host,
                        "arc_chars": len(arc_md),
                        "scrape_chars": len(result.markdown),
                    },
                )
                result = ExtractionResult(markdown=arc_md, metadata=result.metadata)
        if _effective_chars(result, rule, floor) >= floor:
            return await _maybe_render_full(result, url, settings, rule)
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
            elif attempt.engine == "reader":
                logger.info(
                    "Routing below-floor scrape through the reader proxy",
                    extra={
                        "event": "extraction_reader_route",
                        "host": host,
                        "rule": rule.name if rule is not None else None,
                        "primary_chars": len(result.markdown),
                    },
                )
                try:
                    alt = await reader.fetch(attempt.url, settings)
                except ExtractionError as exc:
                    logger.warning(
                        "Reader proxy attempt failed",
                        extra={
                            "event": "extraction_reader_failed",
                            "fallback": attempt.label,
                            "error": str(exc),
                        },
                    )
                    continue
            else:  # firecrawl re-scrape (googlebot/freedium/custom)
                # These bypass attempts go through Firecrawl. On a direct-engine
                # deploy with no real Firecrawl configured, skip them cleanly rather
                # than POST to a placeholder URL -- FlareSolverr + archive still cover
                # the host. When the engine IS firecrawl, the primary already proved it
                # reachable, so never skip there.
                if settings.EXTRACTION_ENGINE == "direct" and not settings.firecrawl_configured:
                    logger.debug(
                        "Skipping Firecrawl re-scrape: direct engine, no Firecrawl configured",
                        extra={"event": "extraction_firecrawl_skipped", "fallback": attempt.label},
                    )
                    continue
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
                return await _maybe_render_full(alt, url, settings, rule)
            _log_fallback_short(attempt.label, alt_chars, accept_floor)

    # Every other strategy came up short. For a render-rule host, give the render
    # sidecar's own browser a last shot before failing -- its Camoufox can clear a
    # DataDome block that left FlareSolverr below floor.
    rescued = await _render_rescue(url, settings, rule)
    if rescued is not None:
        return rescued

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


# Markers that mean a solved page is only the visible front of an article whose body
# is gated behind an expand click. Matched (case-insensitively) against the result's
# markdown + title, like the FlareSolverr detectors. Best effort: trafilatura may strip
# a button's label, so a per-host render rule is the reliable trigger and this is the
# host-agnostic bonus net.
_TRUNCATION_MARKERS = (
    "expand to continue reading",
    "continue reading",
    "read more",
)


def looks_truncated(result: ExtractionResult) -> bool:
    """True when a solved result still reads like a click-gated teaser."""

    return scan_markers(result, _TRUNCATION_MARKERS)


def _is_render_rule(rule: SourceFallback | None) -> bool:
    """True when the matched Site-override rule selects the render strategy."""

    return rule is not None and rule.proxy == "render"


async def _maybe_render_full(
    result: ExtractionResult, url: str, settings: Settings, rule: SourceFallback | None
) -> ExtractionResult:
    """Post-cascade enrichment. When the render sidecar is configured and the host has a
    render Site-override rule (or the result still looks truncated), drive the sidecar to
    click the expander and keep its body only if it is strictly longer. A ``None`` or
    shorter render leaves ``result`` untouched, so a broken click never loses the body."""

    if not settings.RENDER_URL.strip():
        return result
    if not (_is_render_rule(rule) or looks_truncated(result)):
        return result
    alt = await render.fetch(url, settings)
    if alt is None or len(alt.markdown) <= len(result.markdown):
        return result
    logger.info(
        "Render enriched a truncated article",
        extra={
            "event": "render_enriched",
            "host": urlsplit(url).hostname or "",
            "before_chars": len(result.markdown),
            "after_chars": len(alt.markdown),
        },
    )
    return alt


async def _render_rescue(
    url: str, settings: Settings, rule: SourceFallback | None
) -> ExtractionResult | None:
    """Last resort for a render-rule host whose cascade produced nothing above floor
    (e.g. FlareSolverr was DataDome-blocked). Drive the render sidecar's own browser; if
    it clears the floor, return it instead of failing the job. ``None`` lets the
    too-short error stand. Keyed on the render rule only -- a failed cascade has no
    markdown to auto-detect against."""

    if not settings.RENDER_URL.strip() or not _is_render_rule(rule):
        return None
    alt = await render.fetch(url, settings)
    if alt is None or len(alt.markdown) < settings.MIN_EXTRACTION_CHARS:
        return None
    logger.info(
        "Render rescued a blocked extraction",
        extra={
            "event": "render_rescue",
            "host": urlsplit(url).hostname or "",
            "chars": len(alt.markdown),
        },
    )
    return alt


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
    raw_html = data.get("rawHtml") if isinstance(data.get("rawHtml"), str) else None
    article_chars = jsonld.article_body_chars(raw_html) if raw_html else None
    return ExtractionResult(
        markdown=markdown, metadata=metadata, article_chars=article_chars, raw_html=raw_html
    )


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
    if response.status_code in BLOCKED_STATUS_CODES:
        # A host block forwarded by Firecrawl: route to the bypass cascade, same as
        # the direct engine, rather than dead-ending the job.
        raise ExtractionBlockedError(
            f"Firecrawl rejected request ({response.status_code}): {response.text[:200]}"
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
