"""FlareSolverr extraction engine.

A second fetch engine alongside the Firecrawl client: FlareSolverr runs a real
browser (clearing a Cloudflare/JS challenge, and -- with operator cookies -- fetching
as a logged-in subscriber), and trafilatura pulls the article body out of the solved
HTML. ``extraction.extract`` orchestrates when to use it; this module owns the
solver call, the challenge-page detection, and the HTML->markdown conversion.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.services.extraction_types import ExtractionResult
from app.services.html_markdown import html_to_markdown

logger = logging.getLogger("app.services.flaresolverr")

# Substrings that mark a Cloudflare / bot-challenge interstitial rather than a real
# article. Matched case-insensitively against a below-floor scrape's markdown +
# title; chosen to be specific to challenge pages so a real (short) article rarely
# collides. A detected challenge is one of the signals that routes a scrape to the
# solver, so a plain paywall teaser never pays for a browser.
_CHALLENGE_MARKERS = (
    "just a moment...",
    "attention required! | cloudflare",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "cf-browser-verification",
    "challenge-platform",
    "cloudflare ray id",
    "verify you are human",
    "verify you are a human",
    "ddos-guard",
    "pardon our interruption",
    "access to this page has been denied",
    "sorry, you have been blocked",
)


def looks_like_challenge(result: ExtractionResult) -> bool:
    """True when a below-floor scrape looks like a Cloudflare/bot-challenge page
    (the case FlareSolverr is meant to solve) rather than a real short article."""

    haystack = f"{result.markdown} {result.metadata.get('title', '')}".lower()
    return any(marker in haystack for marker in _CHALLENGE_MARKERS)


# Visible copy of an INTERACTIVE CAPTCHA gate (DataDome/PerimeterX slider,
# hCaptcha/reCAPTCHA) -- the case FlareSolverr cannot auto-solve, so even a "solved"
# page is still a wall. Matched (like the challenge markers) against a below-floor
# scrape's extracted markdown + title, not the raw HTML: vendor sensor scripts
# ("perimeterx", "captcha-delivery.com") ship on plenty of legit pages, so scanning the
# full HTML would drop real articles. Only the human-readable gate text survives
# extraction, and only when the page is too short to be the article.
_CAPTCHA_MARKERS = (
    "verification required",
    "slide right to secure",
    "unusual activity from your device",
    "please verify you are a human",
    "complete the security check to access",
)


def looks_like_captcha(result: ExtractionResult) -> bool:
    """True when a below-floor scrape's extracted text is an interactive CAPTCHA gate
    (DataDome/PerimeterX/etc.) rather than the article -- a wall FlareSolverr can't get
    through. Mirrors ``looks_like_challenge``: scans markdown + title, not raw HTML."""

    haystack = f"{result.markdown} {result.metadata.get('title', '')}".lower()
    return any(marker in haystack for marker in _CAPTCHA_MARKERS)


def _parse_cookies(cookie_string: str, url: str) -> list[dict[str, str]]:
    """Parse a raw ``name=value; name2=value2`` Cookie string into FlareSolverr's
    ``[{name, value, domain}]`` shape, with the request URL's host as the domain."""

    host = (urlsplit(url).hostname or "").lower()
    out: list[dict[str, str]] = []
    for part in cookie_string.split(";"):
        name, sep, value = part.strip().partition("=")
        name = name.strip()
        if name and sep:
            out.append({"name": name, "value": value.strip(), "domain": host})
    return out


async def fetch(url: str, settings: Settings, cookies: str = "") -> ExtractionResult | None:
    """Solve and fetch ``url`` through FlareSolverr, returning article markdown.

    FlareSolverr runs a real browser to clear a Cloudflare/JS challenge and hands
    back the solved HTML; trafilatura pulls the article body out of it. ``cookies``
    (the operator's raw session for the host) is forwarded so a paid subscriber can
    fetch gated content. Any failure (unset URL, solver error, non-200 target, empty
    extraction) returns ``None`` so the caller falls through to the too-short error --
    this never raises, so a flaky solver can't turn a clean teaser-fail into a stack
    trace. Uses its own client (no Firecrawl bearer/timeout) and matches the public
    FlareSolverr ``/v1`` shape.
    """

    endpoint = settings.FLARESOLVERR_URL.strip().rstrip("/")
    if not endpoint:
        return None
    if not endpoint.endswith("/v1"):
        endpoint = f"{endpoint}/v1"
    # The read budget must exceed the solver's own maxTimeout so we don't cancel
    # it mid-solve; +30s covers browser spin-up plus our network hop. Connect is
    # kept short so an unreachable solver fails fast instead of stalling the
    # worker for the whole read budget.
    read_timeout = settings.FLARESOLVERR_MAX_TIMEOUT_MS / 1000 + 30
    timeout = httpx.Timeout(read_timeout, connect=10.0)
    payload: dict[str, Any] = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": settings.FLARESOLVERR_MAX_TIMEOUT_MS,
    }
    parsed_cookies = _parse_cookies(cookies, url) if cookies else []
    if parsed_cookies:
        payload["cookies"] = parsed_cookies
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(endpoint, json=payload)
    except httpx.HTTPError as exc:
        logger.warning(
            "FlareSolverr request failed",
            extra={"event": "flaresolverr_unreachable", "error": str(exc)},
        )
        return None

    try:
        body = response.json()
    except ValueError:
        logger.warning("FlareSolverr returned non-JSON", extra={"event": "flaresolverr_bad_response"})
        return None
    if not isinstance(body, dict) or body.get("status") != "ok":
        message = body.get("message") if isinstance(body, dict) else None
        logger.warning(
            "FlareSolverr did not solve the challenge",
            # "message" is a reserved LogRecord field; the solver's own message rides
            # in "solver_message" so structured logging never crashes on it.
            extra={"event": "flaresolverr_error", "solver_message": message},
        )
        return None

    # A truthy non-dict solution (list/str from a malformed or hostile solver)
    # would make solution.get(...) raise AttributeError -- guard it like `body`.
    solution = body.get("solution")
    if not isinstance(solution, dict):
        logger.warning("FlareSolverr returned no solution", extra={"event": "flaresolverr_no_solution"})
        return None
    if solution.get("status") != 200:
        logger.warning(
            "FlareSolverr fetched a non-200 page",
            extra={"event": "flaresolverr_target_status", "status": solution.get("status")},
        )
        return None
    html = solution.get("response")
    if not isinstance(html, str):
        logger.warning("FlareSolverr response was not HTML text", extra={"event": "flaresolverr_bad_html"})
        return None

    markdown, metadata = html_to_markdown(html)
    if not markdown:
        logger.warning(
            "FlareSolverr HTML yielded no article text",
            extra={"event": "flaresolverr_empty_extract"},
        )
        return None

    result = ExtractionResult(markdown=markdown, metadata=metadata)
    if len(markdown) < settings.MIN_EXTRACTION_CHARS and looks_like_captcha(result):
        # FlareSolverr cleared the JS challenge but the host escalated to an interactive
        # CAPTCHA (e.g. inc.com's DataDome slider) it cannot solve. The extracted text is
        # below floor and reads as the gate, not the article -- log the real cause so a
        # failed extraction reads as "blocked by CAPTCHA", not "no text".
        logger.warning(
            "FlareSolverr reached an interactive CAPTCHA; cannot auto-solve",
            extra={"event": "flaresolverr_captcha", "host": urlsplit(url).hostname or ""},
        )
        return None
    return result
