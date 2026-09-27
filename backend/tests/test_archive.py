from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from app.config import get_settings
from app.services import archive


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _cdx(*timestamps: str) -> httpx.Response:
    # CDX json: a field-name header row, then one row per capture.
    return httpx.Response(200, json=[["timestamp"], *[[t] for t in timestamps]])


def _article_html(*, article_body: str | None = None) -> str:
    body = "".join(
        f"<p>Paragraph {i} of the real article body, with enough words for trafilatura "
        f"to keep it as genuine content rather than navigation chrome.</p>"
        for i in range(40)
    )
    head = "<title>Archived</title>"
    if article_body:
        head += (
            '<script type="application/ld+json">'
            + json.dumps({"@type": "NewsArticle", "articleBody": article_body})
            + "</script>"
        )
    return f"<html><head>{head}</head><body><article>{body}</article></body></html>"


def _solver_ok(html: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"status": "ok", "solution": {"url": "x", "status": 200, "response": html, "userAgent": "ua"}},
    )


async def test_wayback_capture_is_extracted(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/cdx/" in request.url.path:
            return _cdx("20260101000000", "20260608120000")
        return httpx.Response(200, text=_article_html())  # the id_ raw capture

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))
    result = await archive.fetch("https://gated.test/post", get_settings())
    assert result is not None
    assert "real article body" in result.markdown


async def test_no_wayback_and_no_solver_returns_none(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLARESOLVERR_URL", "")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/cdx/" in request.url.path:
            return _cdx()  # header only, no captures
        raise AssertionError("must not fetch a capture when there are no snapshots")

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))
    assert await archive.fetch("https://gated.test/post", get_settings()) is None


async def test_archive_today_via_flaresolverr_when_wayback_empty(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No Wayback capture, but archive.today is fetched through the solver (no cookies).
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "web.archive.org":
            return _cdx()  # no captures
        if request.url.host == "flaresolverr":
            return _solver_ok(_article_html())
        raise AssertionError(f"unexpected host {request.url.host}")

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))
    result = await archive.fetch("https://gated.test/post", get_settings())
    assert result is not None
    assert "real article body" in result.markdown


async def test_wayback_capture_carries_jsonld_article_chars(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = _article_html(article_body="short")

    def handler(request: httpx.Request) -> httpx.Response:
        if "/cdx/" in request.url.path:
            return _cdx("20260608120000")
        return httpx.Response(200, text=html)

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))
    result = await archive.fetch("https://gated.test/post", get_settings())
    assert result is not None and result.article_chars == len("short")
