from __future__ import annotations

import logging

import httpx
import pytest
from app.config import get_settings
from app.services import flaresolverr
from app.services.extraction_types import ExtractionResult

# A DataDome-style interactive CAPTCHA gate (what inc.com escalates to) -- FlareSolverr
# clears the JS challenge but lands on this, which it cannot solve. The body is short
# enough to extract below the 500-char floor while still carrying the gate copy.
_CAPTCHA_HTML = (
    "<html><head><title>inc.com</title></head><body>"
    "<h1>Verification Required</h1>"
    "<p>We detected unusual activity from your device or network. "
    "Please verify you are a human by completing the challenge below.</p>"
    "<p>Slide right to secure your access and continue to the page you requested.</p>"
    "</body></html>"
)

_ARTICLE_HTML = (
    "<html><head><title>Real Article</title></head><body><article><h1>Headline</h1><p>"
    + ("word " * 200)
    + "</p></article></body></html>"
)


def _result(markdown: str, title: str = "") -> ExtractionResult:
    return ExtractionResult(markdown=markdown, metadata={"title": title})


def test_looks_like_captcha_detects_gate_not_article() -> None:
    gate = _result(
        "We detected unusual activity from your device. Slide right to secure your access.",
        title="Verification Required",
    )
    article = _result("word " * 200, title="Real Article")
    assert flaresolverr.looks_like_captcha(gate)
    assert not flaresolverr.looks_like_captcha(article)


def _patch_solver(monkeypatch: pytest.MonkeyPatch, html: str) -> None:
    response = httpx.Response(
        200, json={"status": "ok", "solution": {"status": 200, "response": html}}
    )
    transport = httpx.MockTransport(lambda _req: response)
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def test_fetch_returns_none_and_logs_on_captcha(
    env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_solver(monkeypatch, _CAPTCHA_HTML)
    with caplog.at_level(logging.WARNING, logger="app.services.flaresolverr"):
        result = await flaresolverr.fetch("https://www.inc.com/some/article", get_settings())
    assert result is None  # a CAPTCHA gate is a failed solve, not article text
    assert any(getattr(r, "event", "") == "flaresolverr_captcha" for r in caplog.records)


async def test_fetch_extracts_a_real_solved_article(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_solver(monkeypatch, _ARTICLE_HTML)
    result = await flaresolverr.fetch("https://blog.test/post", get_settings())
    assert result is not None
    assert "word" in result.markdown


async def test_fetch_keeps_long_article_that_mentions_a_marker(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An above-floor article that happens to discuss "verification required" must not be
    # dropped as a CAPTCHA: the gate only fires below the extraction floor.
    html = (
        "<html><head><title>On Bot Walls</title></head><body><article><h1>Bot Walls</h1><p>"
        "This piece is about how a verification required gate works. "
        + ("detail " * 200)
        + "</p></article></body></html>"
    )
    _patch_solver(monkeypatch, html)
    result = await flaresolverr.fetch("https://blog.test/bot-walls", get_settings())
    assert result is not None
    assert "verification required" in result.markdown.lower()
