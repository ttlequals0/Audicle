"""Render-sidecar extraction client.

A third fetch path alongside Firecrawl and FlareSolverr: the render sidecar runs
a headful browser, clicks an "EXPAND TO CONTINUE READING"-style control, and hands
back the fully expanded HTML; trafilatura pulls the article body out of it.
``extraction.extract`` decides when to use it (a host whose Site-override rule is the
render strategy, or a solved page that still looks truncated -- as enrichment on a
partial and as a rescue when the cascade fails); this module owns the HTTP call, the
sidecar's
status handling, and the HTML->markdown conversion. It never raises, so a flaky
sidecar can't turn a usable partial into a stack trace.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.services.extraction_types import ExtractionResult
from app.services.html_markdown import html_to_markdown

logger = logging.getLogger("app.services.render")


async def fetch(url: str, settings: Settings) -> ExtractionResult | None:
    """Render ``url`` through the sidecar and return the expanded article markdown.

    Returns ``None`` (never raises) on any failure -- unset URL, sidecar error,
    CAPTCHA wall, empty extraction -- so the caller keeps whatever the cascade
    already had. Uses its own client and matches the sidecar's ``/render`` shape.
    """

    endpoint = settings.RENDER_URL.strip().rstrip("/")
    if not endpoint:
        return None
    if not endpoint.endswith("/render"):
        endpoint = f"{endpoint}/render"
    # The read budget must exceed the sidecar's own browser work (nav + clicks);
    # connect stays short so an unreachable sidecar fails fast.
    timeout = httpx.Timeout(settings.RENDER_TIMEOUT_SECONDS, connect=10.0)
    payload: dict[str, Any] = {"url": url, "expand": True}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(endpoint, json=payload)
    except httpx.HTTPError as exc:
        logger.warning(
            "Render sidecar request failed",
            extra={"event": "render_unreachable", "error": str(exc)},
        )
        return None

    try:
        body = response.json()
    except ValueError:
        logger.warning("Render sidecar returned non-JSON", extra={"event": "render_bad_response"})
        return None
    if not isinstance(body, dict):
        logger.warning("Render sidecar returned no object", extra={"event": "render_bad_response"})
        return None

    status = body.get("status")
    if status != "ok":
        # A CAPTCHA wall is the one failure worth its own event (so a partial reads
        # as "blocked by CAPTCHA"); every other non-ok status is a generic failure.
        logger.warning(
            "Render sidecar did not return article HTML",
            extra={
                "event": "render_captcha" if status == "captcha" else "render_failed",
                "status": status,
                "host": urlsplit(url).hostname or "",
            },
        )
        return None

    html = body.get("html")
    if not isinstance(html, str) or not html:
        logger.warning("Render sidecar returned no HTML", extra={"event": "render_bad_html"})
        return None

    markdown, metadata = html_to_markdown(html)
    if not markdown:
        logger.warning("Render HTML yielded no article text", extra={"event": "render_empty_extract"})
        return None
    return ExtractionResult(markdown=markdown, metadata=metadata)
