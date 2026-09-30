"""The real browser-driving renderer (Camoufox + headful Firefox under xvfb).

Kept in its own module because importing it pulls in Camoufox/Playwright; the
sidecar imports it lazily (only when no renderer is injected), so the pure
``renderer`` helpers and the FastAPI app stay importable -- and testable -- in an
environment that has no browser installed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from urllib.parse import urlsplit

from camoufox.async_api import AsyncCamoufox

from renderer import (
    EMAIL_INPUT_SELECTOR as _EMAIL_INPUT_SELECTOR,
    EXPAND_CLICK_CAP,
    MAX_HTML_CHARS,
    RenderResult,
    RenderOptions,
    default_runtime_config,
    expandable_targets,
    is_captcha_wall,
    is_public_url,
    looks_registration_gated,
    parse_cookie_header,
    registration_form_index,
    word_estimate,
)

logger = logging.getLogger("render.camoufox")

# Set by the stack when the sidecar sits behind the egress proxy.
_PROXY_URL = os.environ.get("RENDER_PROXY_URL")

# Per-page budgets, each overridable from the environment so the loop can be retuned
# without a rebuild. The navigation budget is generous because the page also has to
# clear a DataDome JS challenge; the grow wait gives clicked expanders time to render
# before the body is re-measured.
_DEFAULTS = default_runtime_config()
_NAV_TIMEOUT_MS = _DEFAULTS["RENDER_NAV_TIMEOUT_MS"]
_CLICK_TIMEOUT_MS = _DEFAULTS["RENDER_CLICK_TIMEOUT_MS"]
_GROW_WAIT_MS = _DEFAULTS["RENDER_GROW_WAIT_MS"]
# DataDome's wall is probabilistic and fingerprint-tied: the same page renders the full
# article on one attempt and a CAPTCHA shell (or a stalled nav) on the next. Each attempt
# opens a FRESH Camoufox context (new fingerprint), so retrying re-rolls the challenge.
_RENDER_ATTEMPTS = _DEFAULTS["RENDER_ATTEMPTS"]
# Body-settle polling: accept the page once its visible text stops growing. Analytics
# long-polls keep ``networkidle`` from settling on WSJ-class pages, so a networkidle nav
# timed out at 45s and burned two attempts before a third could re-roll the wall.
_SETTLE_POLL_MS = _DEFAULTS["RENDER_SETTLE_POLL_MS"]
_SETTLE_QUIET_POLLS = _DEFAULTS["RENDER_SETTLE_QUIET_POLLS"]
_SETTLE_MAX_MS = _DEFAULTS["RENDER_SETTLE_MAX_MS"]
# Hard wall-clock cap on the whole retry loop. A request's ``budget_seconds`` (the
# backend's read timeout minus a margin) lowers it further, so the two never drift apart.
_RENDER_BUDGET_SECONDS = _DEFAULTS["RENDER_BUDGET_SECONDS"]
# Only treat these as expand controls. Body prose is never a candidate, so a
# stray "read more" in an article cannot be clicked.
_CONTROL_SELECTOR = "button, a, [role=button]"


def _option(options: RenderOptions | None, name: str, default):
    value = getattr(options, name, None)
    return default if value is None else value


async def _body_len(page) -> int:
    """Visible body text length, measured in the page so only an int crosses the wire."""

    return await page.evaluate("() => document.body ? document.body.innerText.length : 0")


async def _wait_for_document(page, options: RenderOptions | None = None) -> None:
    """Wait out a navigation in flight so the next read sees the new page. Never raises."""

    try:
        timeout = _option(options, "nav_timeout_ms", _NAV_TIMEOUT_MS)
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:  # still loading at the cap; the next read decides
        pass


async def _wait_body_settled(page, options: RenderOptions | None = None) -> None:
    """Poll the visible body length until it stops growing, for at most ``_SETTLE_MAX_MS``.
    Catches late-loading paragraphs without waiting for the network to go quiet. A
    navigation mid-poll (a challenge reload, a link) restarts the count on the new page."""

    max_ms = _option(options, "settle_max_ms", _SETTLE_MAX_MS)
    poll_ms = _option(options, "settle_poll_ms", _SETTLE_POLL_MS)
    quiet_polls = _option(options, "settle_quiet_polls", _SETTLE_QUIET_POLLS)
    deadline = time.monotonic() + max_ms / 1000
    last = -1
    quiet = 0
    while time.monotonic() < deadline:
        try:
            current = await _body_len(page)
        except Exception:  # the page is navigating, or gone
            if page.is_closed():
                return
            await _wait_for_document(page, options)
            await page.wait_for_timeout(poll_ms)
            last, quiet = -1, 0
            continue
        if current and current == last:
            quiet += 1
            if quiet >= quiet_polls:
                return
        else:
            quiet = 0
        last = current
        await page.wait_for_timeout(poll_ms)


async def _wait_body_changed(page, before: int, options: RenderOptions | None = None) -> None:
    """Poll until the body length moves off ``before`` or the page navigates, for at most
    ``_SETTLE_MAX_MS``. A slow form POST leaves the old page steady for seconds."""

    max_ms = _option(options, "settle_max_ms", _SETTLE_MAX_MS)
    poll_ms = _option(options, "settle_poll_ms", _SETTLE_POLL_MS)
    deadline = time.monotonic() + max_ms / 1000
    while time.monotonic() < deadline:
        try:
            if await _body_len(page) != before:
                return
        except Exception:  # the page is navigating
            await _wait_for_document(page, options)
            return
        await page.wait_for_timeout(poll_ms)


async def _run_expand(page, options: RenderOptions | None = None) -> int:
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
                before = await _body_len(page)
                click_timeout = _option(options, "click_timeout_ms", _CLICK_TIMEOUT_MS)
                await control.click(timeout=click_timeout)
            except Exception:  # not clickable / navigated away; try the next target
                continue
            # A beat for the reveal to start; polling at once could lock onto the old page.
            grow_wait = _option(options, "grow_wait_ms", _GROW_WAIT_MS)
            await page.wait_for_timeout(grow_wait)
            await _wait_body_settled(page, options)
            clicks += 1
            try:
                grew = await _body_len(page) > before
            except Exception:  # still navigating past the settle cap; stop expanding
                await _wait_for_document(page, options)
                break
            if grew:
                break  # re-scan from the top: the click may have revealed new controls
        if not grew:
            break
    return clicks


async def _submit_registration(page, email: str, options: RenderOptions | None = None) -> bool:
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
        click_timeout = _option(options, "click_timeout_ms", _CLICK_TIMEOUT_MS)
        await email_input.fill(email, timeout=click_timeout)
        logger.info(
            "submitting a registration gate",
            extra={"event": "render_registration_submit", "host": urlsplit(page.url).hostname},
        )
        before = await _body_len(page)
        await email_input.press("Enter")
        await _wait_body_changed(page, before, options)
        await _wait_body_settled(page, options)
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

    async def render(
        self,
        url: str,
        expand: bool,
        email: str | None = None,
        cookies: str | None = None,
        budget_seconds: float | None = None,
        options: RenderOptions | None = None,
    ) -> RenderResult:
        if not is_public_url(url, proxied=bool(_PROXY_URL)):
            logger.warning(
                "refused non-public render target", extra={"event": "render_blocked_host"}
            )
            return RenderResult(status="error")
        # Bound the whole retry loop so it can't outrun the backend's read timeout. On the
        # cap, report "captcha" -- the backend then keeps whatever the cascade already had,
        # the same outcome as a render that stayed blocked.
        configured_budget = _option(options, "budget_seconds", _RENDER_BUDGET_SECONDS)
        try:
            return await asyncio.wait_for(
                self._render_with_retries(url, expand, email, cookies, options),
                timeout=min(
                    configured_budget,
                    budget_seconds if budget_seconds is not None else configured_budget,
                ),
            )
        except asyncio.TimeoutError:
            logger.warning("render exceeded its time budget", extra={"event": "render_timeout"})
            return RenderResult(status="captcha")

    async def _render_with_retries(
        self,
        url: str,
        expand: bool,
        email: str | None = None,
        cookies: str | None = None,
        options: RenderOptions | None = None,
    ) -> RenderResult:
        # Retry anything short of a usable article: a fresh fingerprint re-rolls DataDome's
        # probabilistic challenge, whether it surfaced as a CAPTCHA shell or a stalled load.
        result = RenderResult(status="error")
        attempts = _option(options, "attempts", _RENDER_ATTEMPTS)
        for attempt in range(1, attempts + 1):
            result, submitted = await self._render_once(
                url, expand, attempt, email, cookies, options
            )
            if submitted:
                # One submission per URL, however many fingerprints the retry loop
                # burns: the publisher gets the address once, not once an attempt.
                email = None
            if result.status == "ok":
                return result
        return result

    async def _render_once(
        self,
        url: str,
        expand: bool,
        attempt: int,
        email: str | None = None,
        cookies: str | None = None,
        options: RenderOptions | None = None,
    ) -> tuple[RenderResult, bool]:
        """The render, plus whether the address was submitted on this attempt."""

        submitted = False
        try:
            proxy = {"server": _PROXY_URL} if _PROXY_URL else None
            async with AsyncCamoufox(headless=False, proxy=proxy) as browser:
                page = await browser.new_page()
                if cookies:
                    await page.context.add_cookies(parse_cookie_header(cookies, url))
                nav_timeout = _option(options, "nav_timeout_ms", _NAV_TIMEOUT_MS)
                await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout)
                await _wait_body_settled(page, options)
                # A JS challenge can hold still, then redirect to the article: give a page
                # that still looks walled one bounded chance to move on before judging it.
                if is_captcha_wall(await page.inner_text("body"), await page.content()):
                    await _wait_body_changed(page, await _body_len(page), options)
                    await _wait_body_settled(page, options)
                clicks = await _run_expand(page, options) if expand else 0
                await _wait_for_document(page, options)
                body_text = await page.inner_text("body")
                html = await page.content()
                # Only now, with the page in front of us and the gate visible, is the
                # operator's address typed anywhere.
                if email and looks_registration_gated(body_text):
                    submitted = await _submit_registration(page, email, options)
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
                    logger.warning(
                        "rendered page exceeds the HTML limit",
                        extra={"event": "render_oversize", "attempt": attempt},
                    )
                    return RenderResult(status="error"), submitted
                words = word_estimate(body_text)
                if is_captcha_wall(body_text, html):
                    logger.warning(
                        "render reached a CAPTCHA gate",
                        extra={"event": "render_captcha", "clicks": clicks, "attempt": attempt},
                    )
                    return RenderResult(
                        status="captcha", clicks=clicks, word_estimate=words
                    ), submitted
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
