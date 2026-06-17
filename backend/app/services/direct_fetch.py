"""Built-in (in-process) extraction engine.

The default primary engine: fetch the article URL directly with httpx and parse it
with trafilatura (via the shared ``html_markdown`` helper), so a fresh deploy needs no
Firecrawl container. Returns the same ``ExtractionResult`` shape as the Firecrawl and
FlareSolverr engines, so the orchestrator's floor check and fallback cascade
(FlareSolverr, web archive, Arc) treat it identically.

The fetch is SSRF-pinned to a validated public IP for the initial request and every
redirect hop (this engine fetches caller-supplied URLs in-process, the same threat
model as the artwork downloader), and the body is size-capped before parsing.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings
from app.services import jsonld, ssrf
from app.services.extraction_types import (
    ExtractionBlockedError,
    ExtractionPermanentError,
    ExtractionResult,
    ExtractionTransientError,
)
from app.services.html_markdown import MAX_HTML_CHARS, html_to_markdown

logger = logging.getLogger("app.services.direct_fetch")

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


async def fetch(url: str, settings: Settings, *, detect_teaser: bool = False) -> ExtractionResult:
    """Fetch ``url`` in-process and return an ``ExtractionResult``. Length is validated
    by the caller. ``detect_teaser`` keeps the raw HTML and records the JSON-LD
    ``articleBody`` length so the Arc extractor and the teaser-floor check can run,
    matching the Firecrawl engine. Transient failures (timeout, 5xx, DNS blip) are
    retried with the same policy as the Firecrawl client; 4xx/SSRF blocks are permanent.
    """

    retrying = AsyncRetrying(
        stop=stop_after_attempt(settings.FIRECRAWL_RETRY_COUNT),
        wait=wait_exponential(
            multiplier=settings.FIRECRAWL_BACKOFF_BASE_SECONDS,
            min=settings.FIRECRAWL_BACKOFF_BASE_SECONDS,
        ),
        retry=retry_if_exception_type(ExtractionTransientError),
        reraise=True,
    )
    html = ""
    async for attempt in retrying:
        with attempt:
            html = await _fetch_html(url, settings)

    markdown, metadata = html_to_markdown(html)
    return ExtractionResult(
        markdown=markdown,
        metadata=metadata,
        article_chars=jsonld.article_body_chars(html) if detect_teaser else None,
        raw_html=html if detect_teaser else None,
    )


async def _fetch_html(url: str, settings: Settings) -> str:
    """One pinned GET of ``url`` -> decoded HTML (size-capped). Raises a typed
    extraction error; the caller's retry loop handles the transient ones."""

    host = urlsplit(url).hostname or ""
    try:
        pinned_ip = await ssrf.resolve_public_host(host)
    except ssrf.BlockedHostError as exc:
        # A confirmed non-public address is a real SSRF hit (permanent); a resolution
        # failure (DNS blip, no records) is transient, so it gets retried.
        if exc.blocked:
            raise ExtractionPermanentError(
                "The article URL resolves to a non-public address and was blocked."
            ) from exc
        raise ExtractionTransientError(f"Could not resolve the article host: {exc.reason}") from exc

    pinned_url = ssrf.pin_url_to_ip(url, pinned_ip)
    headers = {
        "User-Agent": settings.EXTRACTION_DIRECT_USER_AGENT or _BROWSER_UA,
        "Accept": _ACCEPT,
        "Accept-Language": "en-US,en;q=0.9",
        "Host": host,
    }
    timeout = httpx.Timeout(settings.EXTRACTION_DIRECT_TIMEOUT_SECONDS, connect=10.0)
    try:
        async with (
            httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                event_hooks={"request": [ssrf.build_redirect_pin_hook(pinned_ip)]},
            ) as client,
            client.stream(
                "GET", pinned_url, headers=headers, extensions={"sni_hostname": host}
            ) as response,
        ):
            if response.is_server_error:
                raise ExtractionTransientError(
                    f"Direct fetch got {response.status_code} from the article host"
                )
            if response.status_code in (403, 429):
                # A block (forbidden / rate-limited), not a missing page: the same
                # client won't get further, but a bypass might (FlareSolverr from a
                # different IP, or a Wayback capture). Signal the orchestrator to run
                # the fallback cascade rather than failing the job here.
                raise ExtractionBlockedError(
                    f"Direct fetch got {response.status_code} from the article host"
                )
            if response.is_client_error:
                raise ExtractionPermanentError(
                    f"Direct fetch got {response.status_code} from the article host"
                )
            advertised = response.headers.get("Content-Length")
            if advertised is not None:
                try:
                    if int(advertised) > MAX_HTML_CHARS:
                        raise ExtractionPermanentError("The article page exceeds the size cap.")
                except ValueError:
                    pass  # malformed header; the streaming cap below still bounds memory
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > MAX_HTML_CHARS:
                    break  # html_to_markdown rejects oversize bodies; stop reading here
            encoding = response.encoding or "utf-8"
            return bytes(buffer).decode(encoding, errors="replace")
    except ssrf.BlockedHostError as exc:
        # The redirect-pin hook re-resolves each hop and raises here when a redirect
        # (e.g. an open redirect) points at a non-public address -- httpx never connects
        # to it. Convert to the same typed errors as the initial resolve so a blocked
        # redirect fails cleanly instead of escaping as an unhandled RuntimeError.
        if exc.blocked:
            raise ExtractionPermanentError(
                "A redirect from the article URL pointed to a non-public address and was blocked."
            ) from exc
        raise ExtractionTransientError(f"Could not resolve a redirect target: {exc.reason}") from exc
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise ExtractionTransientError(f"Direct fetch could not reach the article host: {exc}") from exc
