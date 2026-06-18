"""Shared HTML -> article-markdown conversion.

Both fetch engines that receive raw HTML -- the FlareSolverr solver and the archive
fallback -- turn it into article markdown plus best-effort title/author/og:image
metadata here, rather than each carrying its own copy of the trafilatura call.
"""

from __future__ import annotations

import logging
from typing import Any

import trafilatura

logger = logging.getLogger("app.services.html_markdown")

# Cap raw HTML before lxml builds a DOM (several times the source size in memory) so a
# pathologically large, attacker-controlled page can't OOM the worker. No real article
# is anywhere near this; the artwork path caps downloads for the same reason.
MAX_HTML_CHARS = 8_000_000


def html_to_markdown(html: str) -> tuple[str, dict[str, Any]]:
    """Extract the main article body from raw HTML as markdown, plus best-effort
    title/author/og:image metadata mapped into the same keys the finalize and artwork
    stages already read from Firecrawl. Returns ``("", {})`` when there is no
    extractable article. Never raises -- the HTML is attacker-controlled."""

    if not html.strip():
        return "", {}
    if len(html) > MAX_HTML_CHARS:
        logger.warning(
            "HTML exceeds the size cap; skipping",
            extra={"event": "html_oversize", "chars": len(html)},
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
        logger.warning("trafilatura could not parse the HTML", extra={"event": "html_parse_error"})
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
