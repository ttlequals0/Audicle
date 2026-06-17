"""Shared extraction types.

``ExtractionResult`` and the typed errors live here so both extraction engines --
the Firecrawl client (``extraction.py``) and the FlareSolverr engine
(``flaresolverr.py``) -- can import them without a circular dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ExtractionResult:
    """Parsed Firecrawl response. ``markdown`` is the cleaned-page body; metadata
    holds anything later phases may want (title, og:image, author).

    ``article_chars`` is the publisher's own declared body length (from the page's
    JSON-LD ``articleBody``) when known. It ignores the related-article and nav chrome
    that can pad a scraped paywall teaser past the floor, so the floor decision can use
    it instead of ``len(markdown)``. ``None`` when the page declares no article body
    (and for the FlareSolverr/archive engines, whose trafilatura bodies are already
    chrome-free)."""

    markdown: str
    metadata: dict[str, Any] = field(default_factory=dict)
    article_chars: int | None = None
    # Raw page HTML, kept only when requested (the teaser/Arc paths) so the Arc XP
    # static body extractor can read content_elements; None otherwise.
    raw_html: str | None = None


class ExtractionError(Exception):
    """Base class so callers can do a single except for any extraction failure."""


class ExtractionTransientError(ExtractionError):
    """5xx, connection refused, timeout. Tenacity retries these."""


class ExtractionPermanentError(ExtractionError):
    """4xx, malformed response, or any other non-retryable failure."""


class ExtractionBlockedError(ExtractionPermanentError):
    """The host refused the request (403/429) -- an IP/WAF/rate block, not a missing
    page. A retry with the same client won't help, but a bypass might (FlareSolverr
    from a different IP, or a Wayback capture), so the orchestrator routes this into
    the fallback cascade instead of failing the job outright. Subclasses
    ``ExtractionPermanentError`` so a caller that doesn't special-case it still treats
    it as non-retryable."""


class ExtractionTooShortError(ExtractionPermanentError):
    """No scrape (direct or fallback) cleared the minimum length.

    The floor is ``MIN_EXTRACTION_CHARS`` by default, or a source-specific
    ``min_chars`` when a ``source_fallbacks`` rule matched the host.
    """
