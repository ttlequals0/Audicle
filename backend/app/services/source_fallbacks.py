"""Source-specific extraction fallbacks.

Some publishers (Medium and lookalikes) gate the article body behind a paywall or
JS, so a direct Firecrawl scrape returns only a teaser -- often enough to clear the
global ``MIN_EXTRACTION_CHARS`` floor but useless to narrate. For those hosts the
extractor retries against a reader-proxy URL derived from the original (e.g. Medium
-> Freedium).

The registry is plain data so adding a new problem source is a one-line entry. Each
rule sets its own ``min_chars`` -- the direct-scrape length below which the proxy
fallback kicks in -- because what counts as a full article is source-specific.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SourceFallback:
    """One problem source: which hosts it covers and how to reach the real body."""

    name: str
    # Matched against the URL host: exact (``medium.com``) or any subdomain
    # (``wesbrown18.medium.com``).
    host_suffixes: tuple[str, ...]
    # Reader-proxy URL templates tried in order; each must contain ``{url}``.
    url_templates: tuple[str, ...]
    # A direct scrape shorter than this (for a matched host) triggers the fallback.
    min_chars: int


# Add new problem sources here over time.
REGISTRY: tuple[SourceFallback, ...] = (
    SourceFallback(
        name="medium-freedium",
        host_suffixes=("medium.com",),
        # Freedium serves the full Medium body; its primary domain is flaky, so the
        # mirror is a second attempt.
        url_templates=(
            "https://freedium.cfd/{url}",
            "https://freedium-mirror.cfd/{url}",
        ),
        # A real Medium article is many KB; the paywall teaser (~1.5 KB) clears the
        # global 500-char floor, so use a higher bar to detect it.
        min_chars=3000,
    ),
)


def match(url: str) -> SourceFallback | None:
    """Return the registry rule whose host suffix matches ``url``, or None."""

    host = (urlsplit(url).hostname or "").lower()
    for rule in REGISTRY:
        if any(host == suffix or host.endswith("." + suffix) for suffix in rule.host_suffixes):
            return rule
    return None


def candidate_urls(rule: SourceFallback, url: str) -> list[tuple[str, str]]:
    """Build ``(label, rewritten_url)`` pairs for a matched rule, in try order."""

    return [
        (f"{rule.name}#{index}", template.format(url=url))
        for index, template in enumerate(rule.url_templates)
    ]
