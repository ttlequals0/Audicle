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
    word_estimate,
)

logger = logging.getLogger("render.camoufox")

# Per-page budgets. The navigation budget is generous because the page also has
# to clear a DataDome JS challenge; the grow wait gives revealed content time to
# render before we re-measure the body.
_NAV_TIMEOUT_MS = 45_000
_CLICK_TIMEOUT_MS = 5_000
_GROW_WAIT_MS = 1_500
# Only treat these as expand controls. Body prose is never a candidate, so a
# stray "read more" in an article cannot be clicked.
_CONTROL_SELECTOR = "button, a, [role=button]"


async def _run_expand(page) -> int:
    """Click expand/read-more controls until the body stops growing or the cap is
    hit. Returns how many clicks were made."""

    clicks = 0
    for _ in range(EXPAND_CLICK_CAP):
        controls = await page.locator(_CONTROL_SELECTOR).all()
        texts: list[str] = []
        for control in controls:
            try:
                texts.append(await control.inner_text())
            except Exception:  # a control detached mid-scan; treat as unmatchable
                texts.append("")
        progressed = False
        for idx in expandable_targets(texts):
            control = controls[idx]
            try:
                if not await control.is_visible():
                    continue
                before = len(await page.inner_text("body"))
                await control.click(timeout=_CLICK_TIMEOUT_MS)
            except Exception:  # not clickable / navigated away; try the next round
                continue
            await page.wait_for_timeout(_GROW_WAIT_MS)
            after = len(await page.inner_text("body"))
            clicks += 1
            progressed = after > before
            break
        if not progressed:
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
                clicks = await _run_expand(page) if expand else 0
                body_text = await page.inner_text("body")
                if is_captcha_gate(body_text):
                    logger.warning(
                        "render reached a CAPTCHA gate",
                        extra={"event": "render_captcha", "clicks": clicks},
                    )
                    return RenderResult(
                        status="captcha", clicks=clicks, word_estimate=word_estimate(body_text)
                    )
                html = await page.content()
                if len(html) > MAX_HTML_CHARS:
                    html = html[:MAX_HTML_CHARS]
                return RenderResult(
                    status="ok", html=html, clicks=clicks, word_estimate=word_estimate(body_text)
                )
        except Exception as exc:
            logger.warning("render failed", extra={"event": "render_error", "error": str(exc)})
            return RenderResult(status="error")
