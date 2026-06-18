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
    async def render(self, url: str, expand: bool) -> RenderResult: ...


def _normalize(text: str) -> str:
    """Collapse whitespace so a multi-line control label ("CONTINUE\\nREADING")
    matches the single-spaced patterns."""

    return re.sub(r"\s+", " ", text).strip()


def expandable_targets(control_texts: list[str]) -> list[int]:
    """Indices of clickable-control labels that look like an expand/read-more
    control. Pass the visible text of each candidate button/link; body prose is
    not a candidate, so a stray "read more" in an article never trips this."""

    return [i for i, text in enumerate(control_texts) if _EXPAND_RE.search(_normalize(text))]


def is_captcha_gate(body_text: str) -> bool:
    """True when the visible body is short and carries CAPTCHA gate copy -- the
    wall itself, not an article that happens to mention "verification required"."""

    if len(body_text) >= CAPTCHA_BODY_FLOOR:
        return False
    haystack = body_text.lower()
    return any(marker in haystack for marker in _CAPTCHA_MARKERS)


def scroll_exhausted(prev_height: int, cur_height: int) -> bool:
    """True when a scroll step did not grow the document -- lazy content has settled,
    so the scroll loop can stop instead of burning its step budget."""

    return cur_height <= prev_height


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
