"""Render-result contract and the browser-agnostic decision helpers.

The sidecar's job is to load a page in a real (headful) browser, click any
"EXPAND TO CONTINUE READING"-style control until the body stops growing, and hand
back the final HTML. The actual browser drive lives in ``camoufox_renderer`` (it
needs Camoufox installed); everything here is pure and unit-testable without a
browser: the ``RenderResult`` shape, the expand-control matcher, the CAPTCHA
detector, and the word estimate.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

# Match the backend's html_markdown cap so an oversize DOM can't be serialized
# into a multi-megabyte response.
MAX_HTML_CHARS = 8_000_000
# Click at most this many expand controls per page -- bounds a pathological page
# that keeps revealing "read more" controls forever.
EXPAND_CLICK_CAP = 3
# Below this many characters of visible body text, a page carrying CAPTCHA gate
# copy is the wall itself, not an article that merely mentions it. Mirrors the
# backend's MIN_EXTRACTION_CHARS floor so the two agree on "this is a gate."
CAPTCHA_BODY_FLOOR = 500

# Visible copy of an expand/read-more control. Applied to clickable control texts
# (buttons/links), not body prose, so a generic "read more" rarely misfires.
_EXPAND_RE = re.compile(
    r"\b(expand|continue reading|read more|show more|see more|view more|load more)\b",
    re.IGNORECASE,
)

# Same visible CAPTCHA-gate strings the backend flaresolverr detector uses, so a
# DataDome/PerimeterX wall reads as "captcha" on both sides.
_CAPTCHA_MARKERS = (
    "verification required",
    "slide right to secure",
    "unusual activity from your device",
    "please verify you are a human",
    "complete the security check to access",
)

# Challenge hosts that serve the bot wall inside an iframe/script. When the wall is an
# iframe (DataDome, hCaptcha, Cloudflare Turnstile) the visible body is near-empty so the
# text markers above never match -- the only signal is the host in the page HTML. Only
# consulted when the body is already below the article floor, so a real article whose page
# merely loads one of these scripts is never misread as a wall.
_CAPTCHA_HTML_MARKERS = (
    "captcha-delivery.com",  # DataDome (inc.com)
    "hcaptcha.com",
    "challenges.cloudflare.com",  # Turnstile
)


@dataclass
class RenderResult:
    """What the sidecar returns for one page render.

    ``status`` is ``ok`` (usable HTML), ``captcha`` (hit a wall it cannot pass), or
    ``error`` (load/click failed, or a non-public host). ``html`` is the final DOM
    on ``ok``, empty otherwise."""

    status: str
    html: str = ""
    clicks: int = 0
    word_estimate: int = 0


class Renderer(Protocol):
    async def render(
        self, url: str, expand: bool, email: str | None = None
    ) -> RenderResult: ...


def _normalize(text: str) -> str:
    """Collapse whitespace so a multi-line control label ("CONTINUE\\nREADING")
    matches the single-spaced patterns."""

    return re.sub(r"\s+", " ", text).strip()


def expandable_targets(control_texts: list[str]) -> list[int]:
    """Indices of clickable-control labels that look like an expand/read-more
    control. Pass the visible text of each candidate button/link; body prose is
    not a candidate, so a stray "read more" in an article never trips this."""

    return [i for i, text in enumerate(control_texts) if _EXPAND_RE.search(_normalize(text))]


# Wording that means the article stops until the reader hands over an email. Kept in
# step with the backend's _GATE_MARKERS so both sides call the same pages gated.
_REGISTRATION_MARKERS = (
    "continue reading this story",
    "sign me up for the newsletter",
    "create your free account",
    "subscribe to continue",
    "to continue reading",
    "this post is for paid subscribers",
    "become a member to read",
    "already have an account",
)
# Field and marker names publishers use on the unlock form. Newspack (WordPress) is
# the one confirmed in the wild; the generic terms cover the rest.
_REGISTRATION_FIELD_RE = re.compile(
    r"newspack_reader_registration|newspack_newsletters_subscribe|newspack_reader",
    re.IGNORECASE,
)
_EMAIL_FIELD_RE = re.compile(r"^(npe|email|email_address|user_email)$", re.IGNORECASE)
# A form that asks for a password is a login, not a signup, whatever its copy says.
_PASSWORD_FIELD_RE = re.compile(r"^(password|pass|pwd|user_password)$", re.IGNORECASE)
# Where to type the address. Mirrors _EMAIL_FIELD_RE, since a publisher may ship the
# field as type=text.
EMAIL_INPUT_SELECTOR = (
    "input[type=email], input[name=npe], input[name=email], "
    "input[name=email_address], input[name=user_email]"
)


def looks_registration_gated(body_text: str) -> bool:
    """True when the visible copy says the rest is behind an email signup."""

    lowered = _normalize(body_text).lower()
    return any(marker in lowered for marker in _REGISTRATION_MARKERS)


def registration_form_index(forms: list[dict]) -> int | None:
    """Index of the form that unlocks the article, or None.

    Each entry is ``{"fields": [input names], "text": visible text}``. A form
    qualifies only when it asks for an email, asks for no password, and carries
    registration wording or a known registration field. A search box, a comment
    form, a login, and a bare footer newsletter box all fail that test, so none of
    them can receive the operator's address."""

    for index, form in enumerate(forms):
        fields = [str(f) for f in form.get("fields", [])]
        if not any(_EMAIL_FIELD_RE.match(f) for f in fields):
            continue
        if any(_PASSWORD_FIELD_RE.match(f) for f in fields):
            continue
        if any(_REGISTRATION_FIELD_RE.search(f) for f in fields) or looks_registration_gated(
            str(form.get("text", ""))
        ):
            return index
    return None


def is_captcha_wall(body_text: str, html: str = "") -> bool:
    """True when the page is a bot wall, not an article -- the signal to retry.

    A wall has almost no visible body. Below the article floor it is a wall if either the
    visible copy carries known gate text, or the HTML embeds a known challenge host
    (DataDome/hCaptcha/Turnstile serve the challenge in an iframe, so the visible body is
    empty and only the host appears in the HTML). The floor guard means a real article --
    which renders far more than the floor of visible text -- is never misread as a wall."""

    if len(body_text) >= CAPTCHA_BODY_FLOOR:
        return False
    if any(marker in body_text.lower() for marker in _CAPTCHA_MARKERS):
        return True
    page = html.lower()
    return any(marker in page for marker in _CAPTCHA_HTML_MARKERS)


def word_estimate(text: str) -> int:
    """Rough word count of visible text, for logging how much the expand added."""

    return len(text.split())


def is_public_url(url: str) -> bool:
    """Defense in depth: refuse to drive the browser at a private/loopback host.

    The backend already validates the URL is public before calling, but the
    sidecar can reach the internal Docker network, so it re-checks every resolved
    address. Returns False on an unparseable host or a DNS failure (fail closed)."""

    host = (urlsplit(url).hostname or "").strip()
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    # ``not is_global`` is the canonical "public address" test: it rejects private,
    # loopback, link-local, multicast, reserved, unspecified AND shared/CGNAT space
    # (100.64.0.0/10), which a hand-rolled predicate list misses. Reject if ANY
    # resolved address is non-global.
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            return False
    return True
