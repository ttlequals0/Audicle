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

from app.config import Settings
from app.services import jsonld, pinned_fetch
from app.services.extraction_types import ExtractionResult
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

    headers = {
        "User-Agent": settings.EXTRACTION_DIRECT_USER_AGENT or _BROWSER_UA,
        "Accept": _ACCEPT,
        "Accept-Language": "en-US,en;q=0.9",
    }
    html = await pinned_fetch.get_text_retrying(
        url,
        settings,
        headers=headers,
        max_bytes=MAX_HTML_CHARS,
        timeout_seconds=settings.EXTRACTION_DIRECT_TIMEOUT_SECONDS,
    )
    markdown, metadata = html_to_markdown(html)
    return ExtractionResult(
        markdown=markdown,
        metadata=metadata,
        article_chars=jsonld.article_body_chars(html) if detect_teaser else None,
        raw_html=html if detect_teaser else None,
    )
