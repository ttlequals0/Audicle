from __future__ import annotations

import logging

import httpx
import pytest
from app.config import get_settings
from app.services import render

_ARTICLE_HTML = (
    "<html><head><title>Full Article</title></head><body><article><h1>Headline</h1><p>"
    + ("word " * 200)
    + "</p></article></body></html>"
)


def _settings(monkeypatch: pytest.MonkeyPatch, url: str = "http://render.test:8000"):
    monkeypatch.setenv("RENDER_URL", url)
    get_settings.cache_clear()
    return get_settings()


def _patch_render(monkeypatch: pytest.MonkeyPatch, body: dict) -> None:
    response = httpx.Response(200, json=body)
    transport = httpx.MockTransport(lambda _req: response)
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def test_fetch_extracts_markdown_on_ok(env, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_render(monkeypatch, {"status": "ok", "html": _ARTICLE_HTML, "clicks": 2})
    result = await render.fetch("https://www.inc.com/some/article", _settings(monkeypatch))
    assert result is not None
    assert "word" in result.markdown


async def test_fetch_returns_none_when_render_url_unset(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Empty RENDER_URL short-circuits before any HTTP client is built; a transport
    # that explodes proves no call is made.
    def boom(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("sidecar must not be called when RENDER_URL is empty")

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: boom(None))
    result = await render.fetch("https://www.inc.com/x", _settings(monkeypatch, url=""))
    assert result is None


async def test_fetch_returns_none_and_logs_on_captcha(
    env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_render(monkeypatch, {"status": "captcha", "html": "", "clicks": 1})
    with caplog.at_level(logging.WARNING, logger="app.services.render"):
        result = await render.fetch("https://www.inc.com/x", _settings(monkeypatch))
    assert result is None
    assert any(getattr(r, "event", "") == "render_captcha" for r in caplog.records)


async def test_fetch_returns_none_on_error_status(
    env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_render(monkeypatch, {"status": "error", "html": ""})
    with caplog.at_level(logging.WARNING, logger="app.services.render"):
        result = await render.fetch("https://www.inc.com/x", _settings(monkeypatch))
    assert result is None
    assert any(getattr(r, "event", "") == "render_failed" for r in caplog.records)


async def test_fetch_returns_none_on_empty_html(env, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_render(monkeypatch, {"status": "ok", "html": ""})
    result = await render.fetch("https://www.inc.com/x", _settings(monkeypatch))
    assert result is None


async def test_fetch_returns_none_on_unreachable(
    env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def raise_error(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    transport = httpx.MockTransport(raise_error)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: original(*a, **{**k, "transport": transport})
    )
    with caplog.at_level(logging.WARNING, logger="app.services.render"):
        result = await render.fetch("https://www.inc.com/x", _settings(monkeypatch))
    assert result is None
    assert any(getattr(r, "event", "") == "render_unreachable" for r in caplog.records)


async def test_fetch_sends_the_cookie_jar_only_when_set(env, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "html": _ARTICLE_HTML})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: original(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    settings = _settings(monkeypatch)
    await render.fetch("https://www.wsj.com/a", settings, cookies="sid=abc")
    await render.fetch("https://www.wsj.com/a", settings)
    assert payloads[0]["cookies"] == "sid=abc"
    assert "cookies" not in payloads[1]


async def test_fetch_tells_the_sidecar_to_stop_before_the_read_timeout(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "html": _ARTICLE_HTML})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: original(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    settings = _settings(monkeypatch)
    settings.RENDER_TIMEOUT_SECONDS = 150.0
    await render.fetch("https://www.wsj.com/a", settings)
    settings.RENDER_TIMEOUT_SECONDS = 5.0
    await render.fetch("https://www.wsj.com/a", settings)
    assert payloads[0]["budget_seconds"] == 140.0
    assert "budget_seconds" not in payloads[1]
