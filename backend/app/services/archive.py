"""Archive fallback extraction engine.

A third fetch engine (alongside the Firecrawl client and FlareSolverr) that pulls an
article from a public web archive when the live page is gated. It tries the Wayback
Machine first -- a clean CDX API and raw-capture URLs with no bot wall, so a plain
httpx fetch works -- then archive.today as a best-effort fallback through FlareSolverr
(archive.today sits behind DDoS-Guard, which the solver can sometimes clear, though
not its captcha). An archived teaser is still a teaser, so the caller holds the result
to the same floor as any other strategy. Never raises.
"""

from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.services import flaresolverr
from app.services.extraction_types import ExtractionResult
from app.services.html_markdown import html_to_markdown

logger = logging.getLogger("app.services.archive")

_CDX_ENDPOINT = "http://web.archive.org/cdx/search/cdx"
# The ``id_`` modifier returns the original captured bytes without the Wayback toolbar.
_WAYBACK_RAW = "https://web.archive.org/web/{timestamp}id_/{url}"
_ARCHIVE_TODAY = "https://archive.ph/newest/{url}"
# Try a few of the most recent captures: the newest may itself be a paywalled grab, an
# older one may predate the wall. Bounded so a fallback can't fan out unboundedly.
_MAX_SNAPSHOTS = 3
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


async def fetch(
    url: str, settings: Settings, *, include_archive_today: bool = True
) -> ExtractionResult | None:
    """Return article markdown from a public archive, or None. Wayback first (no
    cookies, no bot wall); then archive.today via FlareSolverr when allowed and a
    solver is configured. Never raises -- a flaky archive can't crash extraction."""

    result = await _from_wayback(url, settings)
    if result is not None:
        return result
    if include_archive_today:
        return await _from_archive_today(url, settings)
    return None


async def _wayback_timestamps(url: str, settings: Settings) -> list[str]:
    """Newest-first capture timestamps (HTTP 200, content-deduped) for url, or []."""

    params = {
        "url": url,
        "output": "json",
        "filter": "statuscode:200",
        "collapse": "digest",
        "fl": "timestamp",
        "limit": str(-_MAX_SNAPSHOTS),  # the most recent N captures
    }
    timeout = httpx.Timeout(settings.WAYBACK_TIMEOUT_SECONDS, connect=10.0)
    try:
        async with httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": _BROWSER_UA}
        ) as client:
            response = await client.get(_CDX_ENDPOINT, params=params)
    except httpx.HTTPError as exc:
        logger.warning("Wayback CDX query failed", extra={"event": "archive_cdx_error", "error": str(exc)})
        return []
    if response.status_code != 200:
        return []
    try:
        rows = response.json()
    except ValueError:
        return []
    # CDX json is [["timestamp"], ["2026..."], ...] -- the first row is the field header.
    if not isinstance(rows, list) or len(rows) < 2:
        return []
    timestamps = [row[0] for row in rows[1:] if isinstance(row, list) and row]
    timestamps.reverse()  # CDX returns ascending; try the newest capture first
    return timestamps


async def _from_wayback(url: str, settings: Settings) -> ExtractionResult | None:
    timestamps = await _wayback_timestamps(url, settings)
    if not timestamps:
        return None
    timeout = httpx.Timeout(settings.WAYBACK_TIMEOUT_SECONDS, connect=10.0)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, headers={"User-Agent": _BROWSER_UA}
    ) as client:
        for timestamp in timestamps:
            raw_url = _WAYBACK_RAW.format(timestamp=timestamp, url=url)
            try:
                response = await client.get(raw_url)
            except httpx.HTTPError as exc:
                logger.warning(
                    "Wayback capture fetch failed",
                    extra={"event": "archive_fetch_error", "error": str(exc)},
                )
                continue
            if response.status_code != 200:
                continue
            markdown, metadata = html_to_markdown(response.text)
            if markdown:
                logger.info(
                    "Archive fallback used a Wayback capture",
                    extra={
                        "event": "extraction_archive_used",
                        "source": "wayback",
                        "timestamp": timestamp,
                        "markdown_chars": len(markdown),
                    },
                )
                return ExtractionResult(markdown=markdown, metadata=metadata)
    return None


async def _from_archive_today(url: str, settings: Settings) -> ExtractionResult | None:
    """Best-effort archive.today via FlareSolverr (DDoS-Guard, no cookies). Returns
    whatever the solver extracts, or None when no solver is configured."""

    if not settings.FLARESOLVERR_URL.strip():
        return None
    result = await flaresolverr.fetch(_ARCHIVE_TODAY.format(url=url), settings)
    if result is not None and result.markdown:
        logger.info(
            "Archive fallback used an archive.today snapshot",
            extra={
                "event": "extraction_archive_used",
                "source": "archive.today",
                "markdown_chars": len(result.markdown),
            },
        )
    return result
