from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from app.config import get_settings
from app.services import direct_fetch, ssrf
from app.services.extraction_types import ExtractionPermanentError, ExtractionTransientError

_PUBLIC_IP = "203.0.113.7"


@pytest.fixture(autouse=True)
def _stub_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin SSRF resolution to a fixed public IP so the tests never touch DNS."""

    async def _resolve(_host: str) -> str:
        return _PUBLIC_IP

    monkeypatch.setattr(ssrf, "resolve_public_host", _resolve)


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRECRAWL_BACKOFF_BASE_SECONDS", "0")
    monkeypatch.setenv("FIRECRAWL_RETRY_COUNT", "2")
    get_settings.cache_clear()


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _article_html(*, og_image: str | None = None, article_body: str | None = None) -> str:
    body = "".join(
        f"<p>Paragraph {i} of the real article body, with enough words for trafilatura "
        f"to keep it as genuine content rather than navigation chrome.</p>"
        for i in range(40)
    )
    head = "<title>The Headline</title>"
    if og_image:
        head += f'<meta property="og:image" content="{og_image}">'
    if article_body:
        head += (
            '<script type="application/ld+json">'
            + json.dumps({"@type": "NewsArticle", "articleBody": article_body})
            + "</script>"
        )
    return f"<html><head>{head}</head><body><article>{body}</article></body></html>"


async def test_direct_fetch_extracts_article(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_async_client(
        monkeypatch, httpx.MockTransport(lambda req: httpx.Response(200, text=_article_html()))
    )
    result = await direct_fetch.fetch("https://blog.test/post", get_settings())
    assert "real article body" in result.markdown
    assert result.metadata.get("title")


async def test_direct_fetch_pulls_og_image(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    html = _article_html(og_image="https://img.test/cover.jpg")
    _patch_async_client(monkeypatch, httpx.MockTransport(lambda req: httpx.Response(200, text=html)))
    result = await direct_fetch.fetch("https://blog.test/post", get_settings())
    # artwork._extract_og_image reads metadata["ogImage"] first, so the cover survives.
    assert result.metadata.get("ogImage") == "https://img.test/cover.jpg"


async def test_direct_fetch_detect_teaser_sets_raw_html_and_article_chars(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "Declared article body. " * 50
    html = _article_html(article_body=body)
    _patch_async_client(monkeypatch, httpx.MockTransport(lambda req: httpx.Response(200, text=html)))
    result = await direct_fetch.fetch("https://blog.test/post", get_settings(), detect_teaser=True)
    assert result.raw_html == html
    assert result.article_chars == len(body.strip())


async def test_direct_fetch_no_teaser_omits_raw_html(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = _article_html(article_body="x " * 50)
    _patch_async_client(monkeypatch, httpx.MockTransport(lambda req: httpx.Response(200, text=html)))
    result = await direct_fetch.fetch("https://blog.test/post", get_settings(), detect_teaser=False)
    assert result.raw_html is None
    assert result.article_chars is None


async def test_direct_fetch_5xx_is_transient(
    env: Path, monkeypatch: pytest.MonkeyPatch, fast_backoff: None
) -> None:
    _patch_async_client(
        monkeypatch, httpx.MockTransport(lambda req: httpx.Response(503, text="down"))
    )
    with pytest.raises(ExtractionTransientError):
        await direct_fetch.fetch("https://blog.test/post", get_settings())


async def test_direct_fetch_4xx_is_permanent(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_async_client(
        monkeypatch, httpx.MockTransport(lambda req: httpx.Response(404, text="nope"))
    )
    with pytest.raises(ExtractionPermanentError):
        await direct_fetch.fetch("https://blog.test/post", get_settings())


async def test_direct_fetch_blocked_host_is_permanent(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _block(_host: str) -> str:
        raise ssrf.BlockedHostError("blog.test", "non_public_address_10.0.0.1", blocked=True)

    monkeypatch.setattr(ssrf, "resolve_public_host", _block)
    with pytest.raises(ExtractionPermanentError):
        await direct_fetch.fetch("https://blog.test/post", get_settings())


async def test_direct_fetch_blocked_redirect_is_permanent_not_runtime_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A redirect to a private address raises BlockedHostError from the pin hook; it
    must surface as a clean ExtractionPermanentError, not an unhandled RuntimeError."""

    async def _resolve(host: str) -> str:
        if host == "blog.test":
            return _PUBLIC_IP
        raise ssrf.BlockedHostError(host, "non_public_address_127.0.0.1", blocked=True)

    monkeypatch.setattr(ssrf, "resolve_public_host", _resolve)

    def handler(request: httpx.Request) -> httpx.Response:
        # First hop (pinned public IP) 302s to a private host; httpx fires the request
        # hook for the redirect, which re-resolves "internal.test" and blocks.
        return httpx.Response(302, headers={"Location": "https://internal.test/x"})

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))
    with pytest.raises(ExtractionPermanentError):
        await direct_fetch.fetch("https://blog.test/post", get_settings())
