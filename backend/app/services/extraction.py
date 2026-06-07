"""Firecrawl extraction client.

Wraps the self-hosted Firecrawl ``/v1/scrape`` endpoint with tenacity retries on
transient failures and a minimum-length guard. Other stages of the pipeline see
a clean ``ExtractionResult`` or a typed exception.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
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

    For hosts with a known paywall/JS gate (see ``source_fallbacks``), a direct
    scrape that comes back below the source's ``min_chars`` is retried with a bypass
    strategy (re-scrape as Googlebot, a reader-proxy rewrite, or a clean fail) before
    giving up. ``registry`` is the effective rule set (operator config merged over the
    built-ins); ``None`` uses the built-ins only.

    Raises:
        ExtractionTransientError: every retry exhausted on a retryable failure.
        ExtractionPermanentError: 4xx, malformed JSON, or other non-retryable.
        ExtractionTooShortError: no candidate cleared the minimum length.
    """

    # A matched rule raises the bar (teasers clear the global floor) and supplies
    # the bypass attempts. Disabling the feature reverts to plain behavior.
    rule = match(url, registry) if settings.EXTRACTION_FALLBACKS_ENABLED else None
    floor = rule.min_chars if rule else settings.MIN_EXTRACTION_CHARS

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

        # Direct scrape too short for this source: try the bypass attempts. Each
        # attempt is a (target_url, headers) pair -- "googlebot" re-scrapes the same
        # url with crawler headers, a proxy strategy rewrites the url.
        if rule is not None:
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
                if len(alt.markdown) >= floor:
                    logger.info(
                        "Extraction fallback succeeded",
                        extra={
                            "event": "extraction_fallback_used",
                            "fallback": label,
                            "primary_chars": len(result.markdown),
                            "markdown_chars": len(alt.markdown),
                        },
                    )
                    return alt

    floor_desc = f"min_chars={floor} for {rule.name}" if rule else f"MIN_EXTRACTION_CHARS={floor}"
    raise ExtractionTooShortError(
        f"Extracted markdown is {len(result.markdown)} chars, below {floor_desc}"
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
