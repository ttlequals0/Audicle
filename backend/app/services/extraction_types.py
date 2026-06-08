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
