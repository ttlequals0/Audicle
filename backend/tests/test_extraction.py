from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from app.config import get_settings
from app.services import extraction


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force FIRECRAWL_BACKOFF_BASE_SECONDS=0 so tenacity's exponential wait
    returns immediately. Tests that exercise the retry loop request this so
    they don't sleep for whole seconds while waiting for retries."""

    monkeypatch.setenv("FIRECRAWL_BACKOFF_BASE_SECONDS", "0")
    monkeypatch.setenv("FIRECRAWL_RETRY_COUNT", "3")

    from app.config import get_settings

    get_settings.cache_clear()


def _ok_response(markdown: str = "x" * 1000) -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps(
            {"success": True, "data": {"markdown": markdown, "metadata": {"title": "ok"}}}
        ).encode(),
        headers={"content-type": "application/json"},
    )


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    """Swap httpx.AsyncClient for a constructor that pins the test transport."""

    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _stub_transport(*responses) -> httpx.MockTransport:
    """Cycle through the given responses, each consumed once."""

    iterator = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            value = next(iterator)
        except StopIteration as exc:
            raise AssertionError("Extraction made more HTTP calls than expected") from exc
        if isinstance(value, Exception):
            raise value
        return value

    return httpx.MockTransport(handler)


async def test_extract_happy_path(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _stub_transport(_ok_response("body " * 200))

    _patch_async_client(monkeypatch, transport)

    result = await extraction.extract("https://example.test/article", get_settings())
    assert result.markdown.startswith("body ")
    assert result.metadata["title"] == "ok"


async def test_extract_sends_main_content_filtering(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _ok_response("body " * 200)

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))

    await extraction.extract("https://example.test/article", get_settings())
    assert captured["onlyMainContent"] is True
    assert captured["removeBase64Images"] is True
    assert captured["excludeTags"] == ["nav", "footer", "header", "aside"]


async def test_extract_sends_bearer_header_when_key_set(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-secret-123")
    get_settings.cache_clear()
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return _ok_response("body " * 200)

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))
    await extraction.extract("https://example.test/article", get_settings())
    assert captured["auth"] == "Bearer fc-secret-123"


async def test_extract_omits_auth_header_when_key_unset(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    get_settings.cache_clear()
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return _ok_response("body " * 200)

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))
    await extraction.extract("https://example.test/article", get_settings())
    assert captured["auth"] is None


async def test_extract_retries_on_5xx_then_succeeds(
    env: Path, monkeypatch: pytest.MonkeyPatch, fast_backoff
) -> None:
    transport = _stub_transport(
        httpx.Response(500, text="boom"),
        httpx.Response(503, text="still boom"),
        _ok_response("body " * 200),
    )
    _patch_async_client(monkeypatch, transport)

    result = await extraction.extract("https://example.test/article", get_settings())
    assert result.markdown.startswith("body ")


async def test_extract_does_not_retry_on_4xx(
    env: Path, monkeypatch: pytest.MonkeyPatch, fast_backoff
) -> None:
    transport = _stub_transport(httpx.Response(400, text="malformed"))
    _patch_async_client(monkeypatch, transport)

    with pytest.raises(extraction.ExtractionPermanentError, match="400"):
        await extraction.extract("https://example.test/article", get_settings())


async def test_extract_exhausts_retries_on_persistent_5xx(
    env: Path, monkeypatch: pytest.MonkeyPatch, fast_backoff
) -> None:
    transport = _stub_transport(
        httpx.Response(500, text="boom"),
        httpx.Response(500, text="boom"),
        httpx.Response(500, text="boom"),
    )
    _patch_async_client(monkeypatch, transport)

    with pytest.raises(extraction.ExtractionTransientError):
        await extraction.extract("https://example.test/article", get_settings())


async def test_extract_rejects_short_markdown(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _stub_transport(_ok_response("tiny"))
    _patch_async_client(monkeypatch, transport)

    with pytest.raises(extraction.ExtractionTooShortError):
        await extraction.extract("https://example.test/article", get_settings())


async def test_extract_rejects_success_false(
    env: Path, monkeypatch: pytest.MonkeyPatch, fast_backoff
) -> None:
    response = httpx.Response(
        200,
        content=json.dumps({"success": False, "error": "scrape failed"}).encode(),
        headers={"content-type": "application/json"},
    )
    transport = _stub_transport(response)
    _patch_async_client(monkeypatch, transport)

    with pytest.raises(extraction.ExtractionPermanentError, match="success=false"):
        await extraction.extract("https://example.test/article", get_settings())
