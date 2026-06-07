"""Source-specific extraction fallbacks (paywall bypass).

Some publishers gate the article body behind a paywall, so a direct Firecrawl scrape
returns only a teaser -- often enough to clear the global ``MIN_EXTRACTION_CHARS`` floor
but useless to narrate. For a matched host the extractor retries with a bypass strategy.

Strategies (``proxy`` key on a rule):

- ``googlebot`` -- re-scrape the SAME url as Googlebot (UA + ``X-Forwarded-For`` of a
  Googlebot IP). SEO-metered paywalls serve the full article to the crawler. This is the
  built-in "Ladder" technique (github.com/everywall/ladder), implemented natively here
  via the scrape ``headers`` rather than a separate proxy service.
- ``freedium`` -- rewrite the URL to a Freedium reader proxy (best for Medium).
- ``custom`` -- rewrite to an operator-supplied template (must contain ``{url}``).
- ``none`` -- no attempt; a sub-threshold teaser fails the job cleanly.

``BUILTIN`` ships a Medium -> Freedium rule. Operators layer their own host rules on top
(``build_registry``); an operator rule wins over a built-in rule for the same host.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

# The built-in "Ladder" technique: re-scrape the original URL with these headers.
GOOGLEBOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
GOOGLEBOT_XFF = "66.249.66.1"

# Proxy strategy keys offered to operators.
PROXY_KEYS = ("googlebot", "freedium", "custom", "none")

_FREEDIUM_TEMPLATES = ("https://freedium.cfd/{url}", "https://freedium-mirror.cfd/{url}")


@dataclass(frozen=True)
class SourceFallback:
    """One problem source: which hosts it covers and which bypass strategy to use."""

    name: str
    # Matched against the URL host: exact (``medium.com``) or any subdomain.
    host_suffixes: tuple[str, ...]
    proxy: str  # one of PROXY_KEYS
    custom_template: str  # used only when proxy == "custom"
    # A direct scrape shorter than this (for a matched host) triggers the fallback.
    min_chars: int


# Built-in seed: Freedium reliably serves the full Medium body.
BUILTIN: tuple[SourceFallback, ...] = (
    SourceFallback(
        name="medium",
        host_suffixes=("medium.com",),
        proxy="freedium",
        custom_template="",
        # A real Medium article is many KB; the paywall teaser (~1.5 KB) clears the
        # global 500-char floor, so use a higher bar to detect it.
        min_chars=3000,
    ),
)


def match(url: str, registry: tuple[SourceFallback, ...] | None = None) -> SourceFallback | None:
    """Return the first registry rule whose host suffix matches ``url``, or None.

    Defaults to ``BUILTIN`` when no registry is supplied (operator config is built by
    ``build_registry`` and passed in by the extractor).
    """

    rules = BUILTIN if registry is None else registry
    host = (urlsplit(url).hostname or "").lower()
    for rule in rules:
        if any(host == suffix or host.endswith("." + suffix) for suffix in rule.host_suffixes):
            return rule
    return None


def candidate_attempts(
    rule: SourceFallback, url: str
) -> list[tuple[str, str, dict[str, str]]]:
    """Ordered ``(label, target_url, request_headers)`` attempts for the rule's strategy."""

    if rule.proxy == "googlebot":
        # The built-in "Ladder" technique: re-scrape the same URL as Googlebot.
        return [(f"{rule.name}#googlebot", url, {"User-Agent": GOOGLEBOT_UA, "X-Forwarded-For": GOOGLEBOT_XFF})]
    if rule.proxy == "freedium":
        return [
            (f"{rule.name}#freedium{index}", template.format(url=url), {})
            for index, template in enumerate(_FREEDIUM_TEMPLATES)
        ]
    if rule.proxy == "custom" and rule.custom_template:
        return [(f"{rule.name}#custom", rule.custom_template.format(url=url), {})]
    return []  # "none"/reject, or "custom" without a template


def build_registry(
    operator_rules: list[dict[str, str]], default_proxy: str, min_chars: int
) -> tuple[SourceFallback, ...]:
    """Resolve operator rows to ``SourceFallback`` and merge over ``BUILTIN``.

    Operator rules come first so they win on host collision (``match`` returns the first
    match). Each row needs a ``host``; ``proxy`` falls back to ``default_proxy``.
    """

    operator = tuple(
        SourceFallback(
            name=f"operator:{row['host'].lower()}",
            host_suffixes=(row["host"].lower(),),
            proxy=row.get("proxy") or default_proxy,
            custom_template=row.get("custom_template", ""),
            min_chars=min_chars,
        )
        for row in operator_rules
        if row.get("host")
    )
    return operator + BUILTIN
