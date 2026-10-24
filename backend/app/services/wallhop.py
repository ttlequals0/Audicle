"""Wallhop extraction engine (https://wallhop.io, keyless).

Wallhop returns the article's full upstream HTML as text/plain, or the archive.today
copy when the site blocks it, so the body goes through the same in-process parser as a
direct fetch and the caller's length and teaser floors judge it (a block page passed
through with a 200 fails them). Selected per host via the ``wallhop`` strategy and as an
automatic cascade rung.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.services import direct_fetch
from app.services.extraction_types import ExtractionResult
from app.services.proxy_template import build_proxy_url

logger = logging.getLogger("app.services.wallhop")


async def fetch(article_url: str, settings: Settings) -> ExtractionResult:
    """Fetch ``article_url`` through wallhop and return the parsed result. Transient
    failures are retried; 4xx/SSRF blocks are permanent. The caller validates length."""

    proxy_url = build_proxy_url(
        settings.WALLHOP_PROXY_TEMPLATE, article_url, "WALLHOP_PROXY_TEMPLATE"
    )
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
