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


def _flaresolverr_ok(html: str) -> httpx.Response:
    """A FlareSolverr /v1 success: status ok, target 200, solved HTML in solution."""

    return httpx.Response(
        200,
        json={
            "status": "ok",
            "solution": {"url": "x", "status": 200, "response": html, "userAgent": "ua"},
        },
    )


def _gated_article_html() -> str:
    body = "".join(
        f"<p>Paragraph {i} of the real article body, with enough words that trafilatura "
        f"keeps it as genuine content rather than navigation chrome or a cookie banner.</p>"
        for i in range(8)
    )
    head = (
        "<title>Gated Article</title>"
        '<meta property="og:image" content="https://gated.test/cover.jpg">'
        '<meta name="author" content="Jane Doe">'
    )
    return f"<html><head>{head}</head><body><article><h1>Gated Article</h1>{body}</article></body></html>"


def _flaresolverr_registry() -> tuple:
    from app.services.source_fallbacks import SourceFallback

    return (
        SourceFallback(
            name="operator:gated.test",
            host_suffixes=("gated.test",),
            proxy="flaresolverr",
            custom_template="",
            min_chars=200,
        ),
    )


async def test_extract_flaresolverr_fallback_used(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Direct Firecrawl scrape returns a short teaser; the flaresolverr strategy then
    # re-fetches via the solver and trafilatura turns the solved HTML into markdown,
    # mapping title/author/og:image into the keys finalize and artwork expect.
    transport = _stub_transport(_ok_response("short teaser"), _flaresolverr_ok(_gated_article_html()))
    _patch_async_client(monkeypatch, transport)
    result = await extraction.extract(
        "https://gated.test/post", get_settings(), registry=_flaresolverr_registry()
    )
    assert "real article body" in result.markdown
    assert result.metadata.get("title") == "Gated Article"
    assert result.metadata.get("author") == "Jane Doe"
    assert result.metadata.get("ogImage") == "https://gated.test/cover.jpg"


async def test_extract_flaresolverr_malformed_solution_fails_clean(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A solver that returns status ok but a non-dict solution must not raise
    # (the contract is "never raises"); it falls through to the too-short error.
    bad = httpx.Response(200, json={"status": "ok", "solution": ["not", "a", "dict"]})
    transport = _stub_transport(_ok_response("short teaser"), bad)
    _patch_async_client(monkeypatch, transport)
    with pytest.raises(extraction.ExtractionTooShortError):
        await extraction.extract(
            "https://gated.test/post", get_settings(), registry=_flaresolverr_registry()
        )


async def test_extract_flaresolverr_unconfigured_url_fails_clean(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No solver configured: the strategy is skipped (no HTTP call) and the short
    # scrape fails cleanly rather than raising on a missing service.
    monkeypatch.setenv("FLARESOLVERR_URL", "")
    get_settings.cache_clear()
    transport = _stub_transport(_ok_response("short"))  # only the direct scrape
    _patch_async_client(monkeypatch, transport)
    with pytest.raises(extraction.ExtractionTooShortError):
        await extraction.extract(
            "https://gated.test/post", get_settings(), registry=_flaresolverr_registry()
        )


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


_MEDIUM_URL = "https://wesbrown18.medium.com/the-post-abc123"


async def test_extract_medium_falls_back_to_freedium(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Direct Medium scrape returns a teaser (clears the 500 global floor but below
    # the medium rule's 3000 bar); the first Freedium candidate returns the body.
    transport = _stub_transport(_ok_response("teaser " * 100), _ok_response("body " * 1000))
    _patch_async_client(monkeypatch, transport)

    result = await extraction.extract(_MEDIUM_URL, get_settings())
    assert result.markdown.startswith("body ")


async def test_extract_medium_uses_mirror_when_first_fallback_short(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _stub_transport(
        _ok_response("teaser " * 100),  # direct: short
        _ok_response("teaser " * 100),  # freedium.cfd: still short
        _ok_response("body " * 1000),  # freedium-mirror.cfd: full
    )
    _patch_async_client(monkeypatch, transport)

    result = await extraction.extract(_MEDIUM_URL, get_settings())
    assert result.markdown.startswith("body ")


async def test_extract_medium_fallback_disabled_returns_direct(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With fallbacks off, the rule is ignored: a teaser above the 500 global floor
    # is returned as-is (one HTTP call, no Freedium retry).
    monkeypatch.setenv("EXTRACTION_FALLBACKS_ENABLED", "false")
    get_settings.cache_clear()
    transport = _stub_transport(_ok_response("teaser " * 100))
    _patch_async_client(monkeypatch, transport)

    result = await extraction.extract(_MEDIUM_URL, get_settings())
    assert result.markdown.startswith("teaser ")


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


# --- operator registry: googlebot bypass + reject -------------------------------


async def test_extract_googlebot_rescrapes_same_url_with_headers(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import source_fallbacks as sf

    requests: list[httpx.Request] = []
    pages = iter([_ok_response("x" * 100), _ok_response("body " * 1000)])  # teaser, then full

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(pages)

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))
    registry = sf.build_registry(
        [{"host": "washingtonpost.com", "proxy": "googlebot"}],
        default_proxy="googlebot",
        min_chars=3000,
    )
    url = "https://www.washingtonpost.com/a"
    result = await extraction.extract(url, get_settings(), registry)

    assert result.markdown.startswith("body ")
    assert len(requests) == 2
    direct = json.loads(requests[0].content)
    googlebot = json.loads(requests[1].content)
    assert direct["url"] == url and "headers" not in direct
    assert googlebot["url"] == url  # re-scrape the SAME url, not a rewrite
    assert "googlebot" in googlebot["headers"]["User-Agent"].lower()
    assert googlebot["headers"]["X-Forwarded-For"] == "66.249.66.1"


async def test_extract_none_strategy_fails_clean_without_extra_calls(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import source_fallbacks as sf

    transport = _stub_transport(_ok_response("x" * 100))  # only the direct scrape
    _patch_async_client(monkeypatch, transport)
    registry = sf.build_registry(
        [{"host": "wsj.com", "proxy": "none"}], default_proxy="googlebot", min_chars=3000
    )
    with pytest.raises(extraction.ExtractionTooShortError):
        await extraction.extract("https://www.wsj.com/a", get_settings(), registry)
