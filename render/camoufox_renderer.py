"""The real browser-driving renderer (Camoufox + headful Firefox under xvfb).

Kept in its own module because importing it pulls in Camoufox/Playwright; the
sidecar imports it lazily (only when no renderer is injected), so the pure
``renderer`` helpers and the FastAPI app stay importable -- and testable -- in an
environment that has no browser installed.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

from camoufox.async_api import AsyncCamoufox

from renderer import (
    EMAIL_INPUT_SELECTOR as _EMAIL_INPUT_SELECTOR,
    EXPAND_CLICK_CAP,
    MAX_HTML_CHARS,
    RenderResult,
    expandable_targets,
    is_captcha_wall,
    is_public_url,
    looks_registration_gated,
    registration_form_index,
    word_estimate,
)

logger = logging.getLogger("render.camoufox")

# Per-page budgets. The navigation budget is generous because the page also has
# to clear a DataDome JS challenge; the grow wait gives revealed content time to
# render before we re-measure the body.
_NAV_TIMEOUT_MS = 45_000
_CLICK_TIMEOUT_MS = 5_000
_GROW_WAIT_MS = 1_500
# DataDome's wall is probabilistic and fingerprint-tied: the same page renders the full
# article on one attempt and a CAPTCHA shell (or a stalled nav) on the next. Each attempt
# opens a FRESH Camoufox context (new fingerprint), so retrying re-rolls the challenge.
_RENDER_ATTEMPTS = 3
# Hard wall-clock cap on the whole retry loop. The backend's render read timeout is 90s,
# so the sidecar must finish under it (with margin for the HTTP round-trip) or the backend
# discards the render mid-flight. A single attempt can run up to the nav budget, so this
# cap -- not the attempt count -- is what guarantees we stay inside the backend's budget.
_RENDER_BUDGET_SECONDS = 80.0
# Only treat these as expand controls. Body prose is never a candidate, so a
# stray "read more" in an article cannot be clicked.
_CONTROL_SELECTOR = "button, a, [role=button]"


async def _run_expand(page) -> int:
    """Click expand/read-more controls until the body stops growing or the cap is
    hit. Returns how many clicks were made.

    Within a round it keeps trying expand targets until one grows the body, so a
    single dud control (a decoy, or one whose click reveals nothing) does not stop a
    real expander later in the list. A ``seen`` set of control labels prevents
    re-clicking the same persistent "show more" every round and burning the cap on it."""

    clicks = 0
    seen: set[str] = set()
    for _ in range(EXPAND_CLICK_CAP):
        controls = await page.locator(_CONTROL_SELECTOR).all()
        texts: list[str] = []
        for control in controls:
            try:
                texts.append(await control.inner_text())
            except Exception:  # a control detached mid-scan; treat as unmatchable
                texts.append("")
        grew = False
        for idx in expandable_targets(texts):
            label = texts[idx].strip()
            if label in seen:
                continue
            seen.add(label)
            control = controls[idx]
            try:
                if not await control.is_visible():
                    continue
                before = len(await page.inner_text("body"))
                await control.click(timeout=_CLICK_TIMEOUT_MS)
            except Exception:  # not clickable / navigated away; try the next target
                continue
            await page.wait_for_timeout(_GROW_WAIT_MS)
            clicks += 1
            if len(await page.inner_text("body")) > before:
                grew = True
                break  # re-scan from the top: the click may have revealed new controls
        if not grew:
            break
    return clicks


async def _submit_registration(page, email: str) -> bool:
    """Fill the article's unlock form with ``email`` and submit it.

    Returns True when the address was typed and sent, whatever the outcome, so the
    caller can keep it to one submission per URL. ``unlocked`` on the returned page
    is judged separately by the caller. Best effort throughout: any failure leaves
    the page as it was, because a gated article is still worth what it already showed."""

    try:
        forms = await page.locator("form").all()
        described = []
        for form in forms:
            names = await form.locator("input").evaluate_all(
                "els => els.map(e => e.name || e.type || '')"
            )
            described.append({"fields": names, "text": await form.inner_text()})
        index = registration_form_index(described)
        if index is None:
            return False
        email_input = forms[index].locator(_EMAIL_INPUT_SELECTOR).first
        await email_input.fill(email, timeout=_CLICK_TIMEOUT_MS)
        logger.info(
            "submitting a registration gate",
            extra={"event": "render_registration_submit", "host": urlsplit(page.url).hostname},
        )
        await email_input.press("Enter")
        await page.wait_for_load_state("networkidle", timeout=_NAV_TIMEOUT_MS)
        # An AJAX unlock swaps the body in after the network goes idle, so settle
        # before the caller re-reads the page.
        await page.wait_for_timeout(_GROW_WAIT_MS)
        return True
    except Exception:
        logger.warning(
            "registration gate submit failed", extra={"event": "render_registration_failed"}
        )
        return False


class CamoufoxRenderer:
    """Loads a page in a fresh headful Camoufox context, clicks the expander, and
    returns the final HTML. A new context per attempt keeps each render stateless
    (fresh fingerprint, no carried session) -- which is also what lets a retry clear a
    probabilistic DataDome wall."""

    async def render(self, url: str, expand: bool, email: str | None = None) -> RenderResult:
        if not is_public_url(url):
            logger.warning("refused non-public render target", extra={"event": "render_blocked_host"})
            return RenderResult(status="error")
        # Bound the whole retry loop so it can't outrun the backend's read timeout. On the
        # cap, report "captcha" -- the backend then keeps whatever the cascade already had,
        # the same outcome as a render that stayed blocked.
        try:
            return await asyncio.wait_for(
                self._render_with_retries(url, expand, email), timeout=_RENDER_BUDGET_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning("render exceeded its time budget", extra={"event": "render_timeout"})
            return RenderResult(status="captcha")

    async def _render_with_retries(
        self, url: str, expand: bool, email: str | None = None
    ) -> RenderResult:
        # Retry anything short of a usable article: a fresh fingerprint re-rolls DataDome's
        # probabilistic challenge, whether it surfaced as a CAPTCHA shell or a stalled load.
        result = RenderResult(status="error")
        for attempt in range(1, _RENDER_ATTEMPTS + 1):
            result, submitted = await self._render_once(url, expand, attempt, email)
            if submitted:
                # One submission per URL, however many fingerprints the retry loop
                # burns: the publisher gets the address once, not once an attempt.
                email = None
            if result.status == "ok":
                return result
        return result

    async def _render_once(
        self, url: str, expand: bool, attempt: int, email: str | None = None
    ) -> tuple[RenderResult, bool]:
        """The render, plus whether the address was submitted on this attempt."""

        submitted = False
        try:
            async with AsyncCamoufox(headless=False) as browser:
                page = await browser.new_page()
                await page.goto(url, wait_until="networkidle", timeout=_NAV_TIMEOUT_MS)
                clicks = await _run_expand(page) if expand else 0
                body_text = await page.inner_text("body")
                html = await page.content()
                # Only now, with the page in front of us and the gate visible, is the
                # operator's address typed anywhere.
                if email and looks_registration_gated(body_text):
                    submitted = await _submit_registration(page, email)
                    if submitted:
                        after_text = await page.inner_text("body")
                        # Take the post-submit page only when it opened. A longer body,
                        # or the gate copy gone, means the article; anything else is a
                        # "check your inbox" page, and the teaser is worth more.
                        unlocked = len(after_text) > len(body_text) or not looks_registration_gated(
                            after_text
                        )
                        if unlocked:
                            body_text = after_text
                            html = await page.content()
                        logger.info(
                            "registration gate result",
                            extra={"event": "render_registration_done", "unlocked": unlocked},
                        )
                if len(html) > MAX_HTML_CHARS:
                    html = html[:MAX_HTML_CHARS]
                words = word_estimate(body_text)
                if is_captcha_wall(body_text, html):
                    logger.warning(
                        "render reached a CAPTCHA gate",
                        extra={"event": "render_captcha", "clicks": clicks, "attempt": attempt},
                    )
                    return RenderResult(status="captcha", clicks=clicks, word_estimate=words), submitted
                logger.info(
                    "render complete",
                    extra={
                        "event": "render_ok",
                        "clicks": clicks,
                        "attempt": attempt,
                        "word_estimate": words,
                        "html_chars": len(html),
                    },
                )
                return (
                    RenderResult(status="ok", html=html, clicks=clicks, word_estimate=words),
                    submitted,
                )
        except Exception as exc:
            logger.warning(
                "render failed",
                extra={"event": "render_error", "error": str(exc), "attempt": attempt},
            )
            return RenderResult(status="error"), submitted
