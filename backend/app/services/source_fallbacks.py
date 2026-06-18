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
- ``flaresolverr`` -- fetch the URL through FlareSolverr's real browser (see
  ``flaresolverr.fetch``), carrying the rule's cookie jar when set. For hosts that
  hard-block the scraper's IP (e.g. NYT returns 403 to datacenter IPs), where the
  headers-only Googlebot fetch can't help; the operator's solver runs Chrome from a
  residential IP. Emitted as a flaresolverr-engine ``Attempt`` like every strategy, so
  it needs ``FLARESOLVERR_URL`` set.

- ``render`` -- recover the full article body with the headful render sidecar (it clicks
  "EXPAND TO CONTINUE READING"-style gates, e.g. inc.com). Unlike the others this emits no
  loop ``Attempt``; the sidecar runs post-cascade (see ``extraction._maybe_render_full``
  and the too-short rescue) as both enrichment on a partial and a rescue when the cascade
  fails, so a render host always gets the sidecar's stronger browser.

FlareSolverr also runs automatically, for any host, when a below-floor scrape is
detected as a Cloudflare/bot-challenge page (see ``flaresolverr.looks_like_challenge``)
-- that detection-gated path is independent of whether a host selects the
``flaresolverr`` strategy above.

``BUILTIN`` ships a Medium -> Freedium rule plus a render rule for each host in
``config.RENDER_BUILTIN_HOSTS`` (the single place to curate shipped render defaults).
Operators layer their own host rules on top (``build_registry``); an operator rule wins
over a built-in rule for the same host.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.config import RENDER_BUILTIN_HOSTS

# The built-in "Ladder" technique: re-scrape the original URL with these headers.
GOOGLEBOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
GOOGLEBOT_XFF = "66.249.66.1"


@dataclass(frozen=True)
class Attempt:
    """One bypass attempt, engine-tagged so the extractor runs every strategy through
    one loop. ``engine`` is ``"firecrawl"`` (re-scrape ``url`` with ``headers`` -- the
    googlebot/freedium/custom recipes) or ``"flaresolverr"`` (fetch ``url`` through the
    solver's real browser). ``cookies`` is the operator's session for that host, used
    only by the flaresolverr engine (a raw ``name=value; ...`` Cookie string)."""

    label: str
    engine: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    cookies: str = ""
    # True when the operator selected this strategy for the host (via a rule), False for
    # an auto-escalation attempt the extractor synthesised. A host-rule browser/archive
    # grab is held to the rule's teaser floor (an archived/solved teaser is still a
    # teaser); an auto attempt accepts against the hard MIN. Set by ``candidate_attempts``.
    is_host_rule: bool = False

# Proxy strategy keys offered to operators.
PROXY_KEYS = ("googlebot", "freedium", "custom", "none", "flaresolverr", "archive", "render")

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
    # The global-default catch-all: matches any host (lowest priority). Operator
    # and built-in rules above it win on host match; see ``build_registry``.
    catch_all: bool = False
    # Operator's session cookies for this host (raw ``name=value; ...``), sent to the
    # target only via the flaresolverr engine so a paid subscriber can fetch gated
    # content. A secret -- masked in the API, never logged.
    cookies: str = ""


# Render rules for the maintainer-curated hosts in config.RENDER_BUILTIN_HOSTS. The
# render strategy triggers post-cascade regardless of the teaser floor, so min_chars
# is left at 0 (it plays no role for render rules; see extraction._maybe_render_full).
_RENDER_BUILTINS: tuple[SourceFallback, ...] = tuple(
    SourceFallback(
        name=f"render:{host}",
        host_suffixes=(host,),
        proxy="render",
        custom_template="",
        min_chars=0,
    )
    for host in RENDER_BUILTIN_HOSTS
)

# Built-in seed: Freedium reliably serves the full Medium body; plus the render hosts.
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
    *_RENDER_BUILTINS,
)


def match(url: str, registry: tuple[SourceFallback, ...] | None = None) -> SourceFallback | None:
    """Return the first registry rule whose host suffix matches ``url``, or None.

    Defaults to ``BUILTIN`` when no registry is supplied (operator config is built by
    ``build_registry`` and passed in by the extractor).
    """

    rules = BUILTIN if registry is None else registry
    host = (urlsplit(url).hostname or "").lower()
    for rule in rules:
        if rule.catch_all or any(
            host == suffix or host.endswith("." + suffix) for suffix in rule.host_suffixes
        ):
            return rule
    return None


def candidate_attempts(rule: SourceFallback, url: str) -> list[Attempt]:
    """Ordered bypass ``Attempt``s for the rule's strategy (engine-tagged)."""

    if rule.proxy == "googlebot":
        # The built-in "Ladder" technique: re-scrape the same URL as Googlebot.
        headers = {"User-Agent": GOOGLEBOT_UA, "X-Forwarded-For": GOOGLEBOT_XFF}
        return [Attempt(f"{rule.name}#googlebot", "firecrawl", url, headers, is_host_rule=True)]
    if rule.proxy == "freedium":
        return [
            Attempt(
                f"{rule.name}#freedium{index}",
                "firecrawl",
                template.format(url=url),
                is_host_rule=True,
            )
            for index, template in enumerate(_FREEDIUM_TEMPLATES)
        ]
    if rule.proxy == "custom" and rule.custom_template:
        return [
            Attempt(
                f"{rule.name}#custom",
                "firecrawl",
                rule.custom_template.format(url=url),
                is_host_rule=True,
            )
        ]
    if rule.proxy == "flaresolverr":
        # The solver fetches the original URL in a real browser, carrying the rule's cookies.
        return [
            Attempt("host-rule#flaresolverr", "flaresolverr", url, cookies=rule.cookies, is_host_rule=True)
        ]
    if rule.proxy == "archive":
        # Pull the article from a public archive (Wayback, then archive.today). No cookies.
        return [Attempt("host-rule#archive", "archive", url, is_host_rule=True)]
    # "render" emits no loop attempt on purpose: the render sidecar runs post-cascade
    # (extraction._maybe_render_full / the too-short rescue), not as a first-above-floor
    # loop engine. FlareSolverr still auto-escalates to provide a baseline partial.
    return []  # "none"/reject, "custom" without a template, or "render"


def build_registry(
    operator_rules: list[dict[str, str]],
    default_proxy: str,
    min_chars: int,
    global_floor: int = 0,
) -> tuple[SourceFallback, ...]:
    """Resolve operator rows to ``SourceFallback`` and merge over ``BUILTIN``.

    Operator rules come first so they win on host collision (``match`` returns the first
    match). Each row needs a ``host``; ``proxy`` falls back to ``default_proxy``.

    When ``global_floor`` > 0 and ``default_proxy`` is a real strategy (not ``""`` or
    ``"none"``), a lowest-priority catch-all is appended so the default proxy applies
    to *any* host whose scrape is near-empty (below ``global_floor``, the hard
    ``MIN_EXTRACTION_CHARS``). Operator and built-in rules above it win on host match
    and keep their higher teaser floors; a host opts out with a ``proxy="none"`` rule.
    """

    operator = tuple(
        SourceFallback(
            name=f"operator:{row['host'].lower()}",
            host_suffixes=(row["host"].lower(),),
            proxy=row.get("proxy") or default_proxy,
            custom_template=row.get("custom_template", ""),
            min_chars=min_chars,
            cookies=row.get("cookies", ""),
        )
        for row in operator_rules
        if row.get("host")
    )
    registry = operator + BUILTIN
    if global_floor > 0 and default_proxy and default_proxy != "none":
        registry += (
            SourceFallback(
                name=f"global:{default_proxy}",
                host_suffixes=(),
                proxy=default_proxy,
                custom_template="",
                min_chars=global_floor,
                catch_all=True,
            ),
        )
    return registry
