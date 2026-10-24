"""Wallhop extraction engine (https://wallhop.io, no key needed).

Wallhop returns the full upstream HTML of an article as text/plain, falling back to the
archive.today copy when the site blocks it. The HTML goes through the same in-process
parser as a direct fetch (markdown, JSON-LD declared body, Arc), so the caller's length
and teaser floors judge it normally. If wallhop cannot get past the block it returns the
block page verbatim with a 200; those floors catch that too. Selected per host via the
``wallhop`` source-fallback strategy and run as an automatic cascade rung.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.services import direct_fetch
from app.services.extraction_types import ExtractionResult
from app.services.reader import build_proxy_url

logger = logging.getLogger("app.services.wallhop")


async def fetch(article_url: str, settings: Settings) -> ExtractionResult:
    """Fetch ``article_url`` through wallhop and return the parsed result. Transient
    failures are retried; 4xx/SSRF blocks are permanent. The caller validates length."""

    proxy_url = build_proxy_url(settings.WALLHOP_PROXY_TEMPLATE, article_url)
    result = await direct_fetch.fetch(proxy_url, settings, detect_teaser=True)
    logger.info(
        "Wallhop responded",
        extra={
            "event": "wallhop_response",
            "body_chars": len(result.raw_html or ""),
            "markdown_chars": len(result.markdown),
            "article_chars": result.article_chars,
        },
    )
    return result
