"""Tests for the body-settle poll that replaced the networkidle wait."""

from __future__ import annotations

import asyncio

import camoufox_renderer
from camoufox_renderer import _wait_body_changed, _wait_body_settled
from renderer import RenderResult


class _Page:
    """Returns successive body lengths; the last one repeats."""

    def __init__(self, lengths: list[int], fail_at: int | None = None) -> None:
        self.lengths = lengths
        self.fail_at = fail_at
        self.reads = 0
        self.load_waits = 0

    async def evaluate(self, _js: str) -> int:
        i = self.reads
        self.reads += 1
        if i == self.fail_at:
            raise RuntimeError("Execution context was destroyed")
        return self.lengths[min(i, len(self.lengths) - 1)]

    async def wait_for_load_state(self, _state: str, timeout: int | None = None) -> None:
        self.load_waits += 1

    def is_closed(self) -> bool:
        return False

    async def wait_for_timeout(self, _ms: int) -> None:
        return None


async def test_waits_for_a_late_tail_then_stops_once_quiet() -> None:
    page = _Page([100, 100, 900, 900, 900])

    await _wait_body_settled(page)

    # 100, 100 then growth resets the quiet count; 900 must repeat twice more.
    assert page.reads == 5


async def test_an_empty_body_never_counts_as_settled() -> None:
    page = _Page([0])

    await _wait_body_settled(page)

    # Ran to the cap (8 s at 100 ms a clock read) instead of accepting an empty page.
    assert page.reads > 10


async def test_a_zero_poll_interval_still_ends(monkeypatch) -> None:
    monkeypatch.setattr(camoufox_renderer, "_SETTLE_POLL_MS", 0)
    page = _Page([0])

    await _wait_body_settled(page)

    assert page.reads > 1


async def test_a_navigation_restarts_the_count_on_the_new_page() -> None:
    page = _Page([100, 0, 900, 900, 900], fail_at=1)

    await _wait_body_settled(page)

    assert page.load_waits == 1
    assert page.reads == 5


async def test_a_closed_page_stops_the_poll() -> None:
    page = _Page([100], fail_at=0)
    page.is_closed = lambda: True

    await _wait_body_settled(page)

    assert page.reads == 1
    assert page.load_waits == 0


async def test_a_slow_submit_is_waited_out_before_settling() -> None:
    page = _Page([50, 50, 50, 50, 700])

    await _wait_body_changed(page, 50)

    assert page.reads == 5


async def test_a_submit_that_navigates_waits_for_the_new_document() -> None:
    page = _Page([50], fail_at=0)

    await _wait_body_changed(page, 50)

    assert page.load_waits == 1


async def test_the_callers_budget_caps_the_retry_loop() -> None:
    class _Slow(camoufox_renderer.CamoufoxRenderer):
        async def _render_with_retries(self, url, expand, email=None, cookies=None):
            await asyncio.sleep(5)
            return RenderResult(status="ok")

    result = await _Slow().render("https://93.184.216.34/a", True, budget_seconds=0.05)

    assert result.status == "captcha"
