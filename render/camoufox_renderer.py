"""The real browser-driving renderer (Camoufox + headful Firefox under xvfb).

Kept in its own module because importing it pulls in Camoufox/Playwright; the
sidecar imports it lazily (only when no renderer is injected), so the pure
``renderer`` helpers and the FastAPI app stay importable -- and testable -- in an
environment that has no browser installed.
"""

from __future__ import annotations

import logging

from camoufox.async_api import AsyncCamoufox

from renderer import (
    EXPAND_CLICK_CAP,
    MAX_HTML_CHARS,
    RenderResult,
    expandable_targets,
    is_captcha_gate,
    is_public_url,
    scroll_exhausted,
    word_estimate,
)

logger = logging.getLogger("render.camoufox")

# Per-page budgets. The navigation budget is generous because the page also has
# to clear a DataDome JS challenge; the grow wait gives revealed content time to
# render before we re-measure the body.
_NAV_TIMEOUT_MS = 45_000
_CLICK_TIMEOUT_MS = 5_000
_GROW_WAIT_MS = 1_500
# Scroll the page to the bottom in viewport steps so lazy-mounted article paragraphs
# (and below-the-fold expand gates) load before capture. Capped so an infinite
# "recommended articles" feed can't scroll forever; the settle wait lets each revealed
# chunk render before we re-measure the height.
_SCROLL_STEP_CAP = 15
_SCROLL_SETTLE_MS = 800
# Only treat these as expand controls. Body prose is never a candidate, so a
# stray "read more" in an article cannot be clicked.
_CONTROL_SELECTOR = "button, a, [role=button]"


async def _scroll_to_load(page) -> int:
    """Scroll to the bottom in viewport steps until the document stops growing or the
    step cap is hit, then return to the top. Lazy-loading sites (inc.com) only mount
    paragraphs as they near the viewport, so without this the article tail never enters
    the DOM and cannot be captured. Returns how many steps grew the page.

    Defensive: a failed scroll eval degrades to "no scroll" rather than killing the
    render -- the caller still captures whatever loaded."""

    steps = 0
    prev_height = 0
    # Use the larger of body/documentElement: on sites whose scroll container is the
    # <html> element, document.body.scrollHeight under-reports and would stop the loop
    # before the tail loads.
    height_js = "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
    try:
        for _ in range(_SCROLL_STEP_CAP):
            height = int(await page.evaluate(height_js))
            if scroll_exhausted(prev_height, height):
                break
            prev_height = height
            await page.evaluate(f"window.scrollTo(0, {height_js})")
            await page.wait_for_timeout(_SCROLL_SETTLE_MS)
            steps += 1
        # Back to the top so the expand-control visibility checks behave predictably.
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:  # a scroll eval can race a navigation; keep what loaded
        logger.warning("scroll-to-load failed", extra={"event": "render_scroll_error"})
    return steps


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


class CamoufoxRenderer:
    """Loads a page in a fresh headful Camoufox context, clicks the expander, and
    returns the final HTML. A new context per call keeps each render stateless
    (fresh fingerprint, no carried session)."""

    async def render(self, url: str, expand: bool) -> RenderResult:
        if not is_public_url(url):
            logger.warning("refused non-public render target", extra={"event": "render_blocked_host"})
            return RenderResult(status="error")
        try:
            async with AsyncCamoufox(headless=False) as browser:
                page = await browser.new_page()
                await page.goto(url, wait_until="networkidle", timeout=_NAV_TIMEOUT_MS)
                # Load lazy content and surface any below-the-fold gate, click the gate,
                # then load whatever the gate revealed -- so the full body is in the DOM.
                scrolls = await _scroll_to_load(page)
                clicks = await _run_expand(page) if expand else 0
                # Only re-scroll when a gate was actually clicked: otherwise the DOM is
                # unchanged from the first scroll and a second pass is wasted work.
                if clicks:
                    scrolls += await _scroll_to_load(page)
                body_text = await page.inner_text("body")
                words = word_estimate(body_text)
                if is_captcha_gate(body_text):
                    logger.warning(
                        "render reached a CAPTCHA gate",
                        extra={"event": "render_captcha", "clicks": clicks, "scrolls": scrolls},
                    )
                    return RenderResult(status="captcha", clicks=clicks, word_estimate=words)
                html = await page.content()
                if len(html) > MAX_HTML_CHARS:
                    html = html[:MAX_HTML_CHARS]
                logger.info(
                    "render complete",
                    extra={
                        "event": "render_ok",
                        "clicks": clicks,
                        "scrolls": scrolls,
                        "word_estimate": words,
                        "html_chars": len(html),
                    },
                )
                return RenderResult(status="ok", html=html, clicks=clicks, word_estimate=words)
        except Exception as exc:
            logger.warning("render failed", extra={"event": "render_error", "error": str(exc)})
            return RenderResult(status="error")
