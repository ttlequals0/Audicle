from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest
from app.config import get_settings
from app.services import extraction, flaresolverr


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force FIRECRAWL_BACKOFF_BASE_SECONDS=0 so tenacity's exponential wait
    returns immediately. Tests that exercise the retry loop request this so
    they don't sleep for whole seconds while waiting for retries."""

    monkeypatch.setenv("FIRECRAWL_BACKOFF_BASE_SECONDS", "0")
    monkeypatch.setenv("FIRECRAWL_RETRY_COUNT", "3")

    from app.config import get_settings

    get_settings.cache_clear()


@pytest.fixture
def no_flaresolverr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the automatic FlareSolverr escalation so a test can exercise the plain
    or proxy path on a near-empty scrape without the solver pre-empting it (a
    near-empty scrape auto-routes to FlareSolverr when FLARESOLVERR_URL is set)."""

    monkeypatch.setenv("FLARESOLVERR_URL", "")
    from app.config import get_settings

    get_settings.cache_clear()


@pytest.fixture
def no_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the automatic Wayback last-resort so a hard-block test can assert its
    exact call sequence without the archive lookup adding an HTTP call (a near-empty
    scrape auto-appends an archive attempt when ARCHIVE_FALLBACK_ENABLED)."""

    monkeypatch.setenv("ARCHIVE_FALLBACK_ENABLED", "false")
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


def _ok_response_with_jsonld(markdown: str, article_body: str) -> httpx.Response:
    """A Firecrawl scrape whose rawHtml carries a JSON-LD articleBody -- used to test
    that a chrome-inflated teaser is judged by the publisher's declared body length."""

    raw_html = (
        '<html><head><script type="application/ld+json">'
        + json.dumps({"@type": "NewsArticle", "articleBody": article_body})
        + "</script></head><body>page</body></html>"
    )
    return httpx.Response(
        200,
        content=json.dumps(
            {
                "success": True,
                "data": {
                    "markdown": markdown,
                    "metadata": {"title": "ok"},
                    "rawHtml": raw_html,
                },
            }
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
    # 40 paragraphs so the solved markdown is well above a 3000-char teaser floor: a
    # host-rule solver result is held to that floor, so the stub must represent the real
    # full article, not a teaser-length body.
    body = "".join(
        f"<p>Paragraph {i} of the real article body, with enough words that trafilatura "
        f"keeps it as genuine content rather than navigation chrome or a cookie banner.</p>"
        for i in range(40)
    )
    head = (
        "<title>Gated Article</title>"
        '<meta property="og:image" content="https://gated.test/cover.jpg">'
        '<meta name="author" content="Jane Doe">'
    )
    return f"<html><head>{head}</head><body><article><h1>Gated Article</h1>{body}</article></body></html>"


def _challenge_response() -> httpx.Response:
    """A Firecrawl scrape that came back as a Cloudflare challenge page (short, with
    challenge markers) -- the only thing that triggers a FlareSolverr escalation."""

    md = "Just a moment... Enable JavaScript and cookies to continue. Cloudflare Ray ID: abc123"
    return httpx.Response(
        200,
        content=json.dumps(
            {"success": True, "data": {"markdown": md, "metadata": {"title": "Just a moment..."}}}
        ).encode(),
        headers={"content-type": "application/json"},
    )


async def test_extract_challenge_page_escalates_to_flaresolverr(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The direct scrape comes back as a Cloudflare challenge page; that is detected
    # and auto-escalated to FlareSolverr (no per-host rule needed). trafilatura turns
    # the solved HTML into markdown, mapping title/author/og:image into the keys
    # finalize and artwork expect.
    transport = _stub_transport(_challenge_response(), _flaresolverr_ok(_gated_article_html()))
    _patch_async_client(monkeypatch, transport)
    result = await extraction.extract("https://gated.test/post", get_settings())
    assert "real article body" in result.markdown
    assert result.metadata.get("title") == "Gated Article"
    assert result.metadata.get("author") == "Jane Doe"
    assert result.metadata.get("ogImage") == "https://gated.test/cover.jpg"


async def test_extract_plain_teaser_does_not_escalate_to_flaresolverr(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.source_fallbacks import SourceFallback

    # A real teaser (>= MIN_EXTRACTION_CHARS, so NOT near-empty) below the rule's 3000
    # floor must NOT trigger the solver even with FLARESOLVERR_URL set -- only the
    # host's googlebot strategy runs. If FlareSolverr were wrongly called it would
    # consume a third response and _stub_transport asserts.
    transport = _stub_transport(_ok_response("word " * 200), _ok_response("word " * 150))
    _patch_async_client(monkeypatch, transport)
    registry = (SourceFallback("operator:gated.test", ("gated.test",), "googlebot", "", 3000),)
    with pytest.raises(extraction.ExtractionTooShortError):
        await extraction.extract("https://gated.test/post", get_settings(), registry=registry)


async def test_extract_challenge_malformed_solution_fails_clean(
    env: Path, monkeypatch: pytest.MonkeyPatch, no_archive
) -> None:
    # A solver that returns status ok but a non-dict solution must not raise (the
    # contract is "never raises"); it falls through to the too-short error.
    bad = httpx.Response(200, json={"status": "ok", "solution": ["not", "a", "dict"]})
    transport = _stub_transport(_challenge_response(), bad)
    _patch_async_client(monkeypatch, transport)
    with pytest.raises(extraction.ExtractionTooShortError):
        await extraction.extract("https://gated.test/post", get_settings())


def test_looks_like_challenge_detects_cloudflare_and_spares_real_articles() -> None:
    challenge = extraction.ExtractionResult(
        markdown="Checking your browser before accessing. Cloudflare Ray ID: x",
        metadata={"title": "Just a moment..."},
    )
    article = extraction.ExtractionResult(
        markdown="A normal short article about kubernetes and verifying releases.",
        metadata={"title": "Release notes"},
    )
    assert flaresolverr.looks_like_challenge(challenge) is True
    assert flaresolverr.looks_like_challenge(article) is False


async def test_extract_logs_which_strategy_ran_and_when_it_falls_short(
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    no_flaresolverr,
    no_archive,
) -> None:
    from app.services.source_fallbacks import SourceFallback

    # Direct scrape AND the googlebot re-scrape both come back short (the hard-
    # paywall case): the bypass ran but didn't help, which must be visible -- the
    # selected strategy and the below-floor result are both logged. (no_flaresolverr so
    # the near-empty scrape exercises googlebot, not the auto solver.)
    transport = _stub_transport(_ok_response("short teaser"), _ok_response("still short"))
    _patch_async_client(monkeypatch, transport)
    registry = (SourceFallback("operator:gated.test", ("gated.test",), "googlebot", "", 3000),)
    with (
        caplog.at_level(logging.INFO, logger="app.services.extraction"),
        pytest.raises(extraction.ExtractionTooShortError),
    ):
        await extraction.extract("https://gated.test/post", get_settings(), registry=registry)
    events = [getattr(r, "event", "") for r in caplog.records]
    assert "extraction_fallback_start" in events
    assert "extraction_fallback_short" in events
    start = next(r for r in caplog.records if getattr(r, "event", "") == "extraction_fallback_start")
    assert start.strategy == "googlebot"  # the log records which strategy was tried


async def test_extract_logs_when_no_rule_matches_the_host(
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    no_flaresolverr,
    no_archive,
) -> None:
    # A short scrape with no matching rule logs that no bypass was even possible,
    # so "why didn't it use a proxy" is answerable from logs. (no_flaresolverr so the
    # near-empty scrape doesn't auto-route to the solver first.)
    transport = _stub_transport(_ok_response("short"))
    _patch_async_client(monkeypatch, transport)
    with (
        caplog.at_level(logging.INFO, logger="app.services.extraction"),
        pytest.raises(extraction.ExtractionTooShortError),
    ):
        await extraction.extract("https://unlisted.test/a", get_settings())
    rec = next(
        r for r in caplog.records if getattr(r, "event", "") == "extraction_no_fallback_rule"
    )
    assert rec.host == "unlisted.test"


async def test_extract_challenge_unconfigured_flaresolverr_fails_clean(
    env: Path, monkeypatch: pytest.MonkeyPatch, no_archive
) -> None:
    # A challenge page is detected but no solver is configured: no HTTP call is made
    # and the scrape fails cleanly rather than raising on a missing service.
    monkeypatch.setenv("FLARESOLVERR_URL", "")
    get_settings.cache_clear()
    transport = _stub_transport(_challenge_response())  # only the direct scrape
    _patch_async_client(monkeypatch, transport)
    with pytest.raises(extraction.ExtractionTooShortError):
        await extraction.extract("https://gated.test/post", get_settings())


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


async def test_extract_rejects_short_markdown(
    env: Path, monkeypatch: pytest.MonkeyPatch, no_flaresolverr, no_archive
) -> None:
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
    env: Path, monkeypatch: pytest.MonkeyPatch, no_flaresolverr
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
    env: Path, monkeypatch: pytest.MonkeyPatch, no_flaresolverr, no_archive
) -> None:
    from app.services import source_fallbacks as sf

    transport = _stub_transport(_ok_response("x" * 100))  # only the direct scrape
    _patch_async_client(monkeypatch, transport)
    registry = sf.build_registry(
        [{"host": "wsj.com", "proxy": "none"}], default_proxy="googlebot", min_chars=3000
    )
    with pytest.raises(extraction.ExtractionTooShortError):
        await extraction.extract("https://www.wsj.com/a", get_settings(), registry)


# --- global default proxy: applies to any host, near-empty trigger --------------


def _global_default_registry(default_proxy: str = "googlebot"):
    # A registry whose only entry is the global-default catch-all (no per-host
    # rules and no built-ins), so an unlisted host exercises the catch-all path.
    from app.services import source_fallbacks as sf

    return tuple(r for r in sf.build_registry([], default_proxy, 3000, global_floor=500) if r.catch_all)


async def test_extract_global_default_proxy_fires_on_near_empty_scrape(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No per-host rule, but a global default proxy is configured (catch-all in the
    # registry): a near-empty direct scrape (below MIN_EXTRACTION_CHARS, the NYT
    # hard-block case) auto-escalates to the global googlebot re-scrape of the same url.
    requests: list[httpx.Request] = []
    pages = iter([_ok_response("x" * 100), _ok_response("body " * 1000)])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(pages)

    # Isolate the catch-all path from the (separate) FlareSolverr branch.
    monkeypatch.setenv("FLARESOLVERR_URL", "")
    get_settings.cache_clear()
    _patch_async_client(monkeypatch, httpx.MockTransport(handler))
    url = "https://unlisted.test/a"
    result = await extraction.extract(url, get_settings(), _global_default_registry())
    assert result.markdown.startswith("body ")
    assert len(requests) == 2
    googlebot = json.loads(requests[1].content)
    assert googlebot["url"] == url  # re-scrape the SAME url
    assert "googlebot" in googlebot["headers"]["User-Agent"].lower()


async def test_extract_global_default_proxy_skips_legit_short_article(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A legitimately short article (>= MIN_EXTRACTION_CHARS) is returned directly
    # even with a global default catch-all -- the proxy fires only on a near-empty
    # scrape, so a single HTTP call is made (the stub asserts on a second).
    monkeypatch.setenv("FLARESOLVERR_URL", "")
    get_settings.cache_clear()
    transport = _stub_transport(_ok_response("word " * 200))  # 1000 chars >= 500 floor
    _patch_async_client(monkeypatch, transport)
    result = await extraction.extract(
        "https://unlisted.test/a", get_settings(), _global_default_registry()
    )
    assert result.markdown.startswith("word ")


async def test_extract_builtin_rule_floor_wins_over_global_catch_all(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With BOTH a per-host rule (builtin Medium, floor 3000) and the global googlebot
    # catch-all (floor 500) in the registry, a Medium teaser above 500 but below 3000
    # must trigger the Medium rule's freedium bypass -- NOT the catch-all -- because
    # match() returns the per-host rule first. Guards against the catch-all hijacking
    # a host that has a higher-floor rule and narrating a teaser as the article.
    from app.services import source_fallbacks as sf

    monkeypatch.setenv("FLARESOLVERR_URL", "")
    get_settings.cache_clear()
    requests: list[httpx.Request] = []
    pages = iter([_ok_response("teaser " * 215), _ok_response("body " * 1000)])  # ~1505 chars

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(pages)

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))
    registry = sf.build_registry([], "googlebot", 3000, global_floor=500)  # Medium builtin + catch-all
    result = await extraction.extract(_MEDIUM_URL, get_settings(), registry)
    assert result.markdown.startswith("body ")
    assert len(requests) == 2
    bypass = json.loads(requests[1].content)
    assert bypass["url"].startswith("https://freedium")  # Medium rule's freedium, not googlebot
    assert "headers" not in bypass  # googlebot would have set crawler headers


# --- per-host flaresolverr strategy: hard-block hosts route through the solver ----


async def test_extract_per_host_flaresolverr_rule_routes_through_solver(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A host that hard-blocks the scraper IP returns a short, NON-challenge body (no
    # Cloudflare markers). A per-host "flaresolverr" rule routes it through the solver
    # anyway -- triggered by the rule, not challenge detection -- and its article is used.
    # min_chars=3000 (the teaser floor) but the solved article is ~1231 chars: a
    # flaresolverr rule is judged against MIN_EXTRACTION_CHARS (500), not the teaser bar,
    # because the solver returns the full page (no teaser to filter).
    transport = _stub_transport(
        _ok_response("Access Denied"),  # direct scrape: short, not a challenge page
        _flaresolverr_ok(_gated_article_html()),  # FlareSolverr solve
    )
    _patch_async_client(monkeypatch, transport)
    from app.services.source_fallbacks import SourceFallback

    registry = (SourceFallback("operator:gated.test", ("gated.test",), "flaresolverr", "", 3000),)
    result = await extraction.extract("https://gated.test/post", get_settings(), registry)
    assert "real article body" in result.markdown
    assert result.metadata.get("title") == "Gated Article"


async def test_extract_flaresolverr_rule_unconfigured_fails_clean(
    env: Path, monkeypatch: pytest.MonkeyPatch, no_archive
) -> None:
    # A flaresolverr rule with no solver configured makes no extra call (its
    # candidate_attempts is empty) and fails cleanly rather than narrating the stub.
    monkeypatch.setenv("FLARESOLVERR_URL", "")
    get_settings.cache_clear()
    transport = _stub_transport(_ok_response("Access Denied"))  # only the direct scrape
    _patch_async_client(monkeypatch, transport)
    from app.services.source_fallbacks import SourceFallback

    registry = (SourceFallback("operator:gated.test", ("gated.test",), "flaresolverr", "", 500),)
    with pytest.raises(extraction.ExtractionTooShortError):
        await extraction.extract("https://gated.test/post", get_settings(), registry)


async def test_extract_no_global_default_keeps_plain_behavior(
    env: Path, monkeypatch: pytest.MonkeyPatch, no_flaresolverr, no_archive
) -> None:
    # No catch-all (default_proxy "none") means no global fallback: a near-empty
    # scrape with no per-host rule fails cleanly without any proxy call.
    transport = _stub_transport(_ok_response("x" * 100))  # only the direct scrape
    _patch_async_client(monkeypatch, transport)
    with pytest.raises(extraction.ExtractionTooShortError):
        await extraction.extract("https://unlisted.test/a", get_settings(), registry=())


# --- 0.26.0: auto-escalate to FlareSolverr on a near-empty (hard-block) scrape ----


async def test_extract_near_empty_auto_routes_to_flaresolverr_without_rule(
    env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The 0.26.0 behavior: a near-empty scrape (a hard 403/IP block) auto-routes to
    # FlareSolverr for ANY host with FLARESOLVERR_URL set -- no per-host rule needed,
    # no challenge markers needed -- logged as trigger="hard-block".
    transport = _stub_transport(
        _ok_response("Access Denied"),  # direct: near-empty, not a challenge page
        _flaresolverr_ok(_gated_article_html()),  # solver gets the full article
    )
    _patch_async_client(monkeypatch, transport)
    with caplog.at_level(logging.INFO, logger="app.services.extraction"):
        result = await extraction.extract("https://hardblock.test/post", get_settings())
    assert "real article body" in result.markdown
    route = next(
        r for r in caplog.records if getattr(r, "event", "") == "extraction_flaresolverr_route"
    )
    assert route.trigger == "hard-block"


async def test_too_short_message_hard_block_no_solver(
    env: Path, monkeypatch: pytest.MonkeyPatch, no_flaresolverr, no_archive
) -> None:
    # Near-empty + no solver configured: the failure tells the operator the fix.
    transport = _stub_transport(_ok_response("Access Denied"))
    _patch_async_client(monkeypatch, transport)
    with pytest.raises(extraction.ExtractionTooShortError, match="Set FLARESOLVERR_URL"):
        await extraction.extract("https://hardblock.test/a", get_settings())


async def test_too_short_message_hard_block_solver_failed(
    env: Path, monkeypatch: pytest.MonkeyPatch, no_archive
) -> None:
    # Near-empty + solver configured but it also comes back empty: distinct message.
    transport = _stub_transport(
        _ok_response("Access Denied"),  # direct: hard block
        _flaresolverr_ok(""),  # solver returns empty HTML -> nothing extracted
    )
    _patch_async_client(monkeypatch, transport)
    with pytest.raises(extraction.ExtractionTooShortError, match="browser bypass couldn't get"):
        await extraction.extract("https://hardblock.test/a", get_settings())


async def test_extract_near_empty_routes_to_solver_before_googlebot_rule(
    env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Precedence: a near-empty scrape on a host that has a googlebot rule still routes
    # to the solver FIRST (hard-block beats the host's googlebot choice, since the IP
    # block makes a headers-only re-fetch futile). The solver gets the article.
    from app.services.source_fallbacks import SourceFallback

    transport = _stub_transport(
        _ok_response("Access Denied"),  # direct: near-empty
        _flaresolverr_ok(_gated_article_html()),  # solver fires before googlebot
    )
    _patch_async_client(monkeypatch, transport)
    registry = (SourceFallback("operator:gated.test", ("gated.test",), "googlebot", "", 3000),)
    with caplog.at_level(logging.INFO, logger="app.services.extraction"):
        result = await extraction.extract("https://gated.test/post", get_settings(), registry)
    assert "real article body" in result.markdown
    route = next(
        r for r in caplog.records if getattr(r, "event", "") == "extraction_flaresolverr_route"
    )
    assert route.trigger == "hard-block"


async def test_too_short_message_teaser(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real teaser (>= MIN_EXTRACTION_CHARS, below the rule floor) is classified as a
    # paywall teaser, not a hard block -- the solver never fires (not near-empty).
    from app.services.source_fallbacks import SourceFallback

    transport = _stub_transport(_ok_response("word " * 200), _ok_response("word " * 200))
    _patch_async_client(monkeypatch, transport)
    registry = (SourceFallback("operator:teaser.test", ("teaser.test",), "googlebot", "", 3000),)
    with pytest.raises(extraction.ExtractionTooShortError, match="Short teaser"):
        await extraction.extract("https://teaser.test/a", get_settings(), registry=registry)


# --- 0.27.0: flaresolverr fires on a teaser, forwards cookies, fresh scrape ----


async def test_extract_flaresolverr_rule_fires_on_a_teaser(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A flaresolverr rule now uses the rule's teaser floor for the direct check, so a
    # teaser (>= MIN but < the 3000 floor) routes to the solver instead of being
    # returned as the stub. Without the fix the 1000-char teaser would be returned.
    from app.services.source_fallbacks import SourceFallback

    transport = _stub_transport(
        _ok_response("word " * 200),  # direct: ~1000-char teaser, >= 500 but < 3000
        _flaresolverr_ok(_gated_article_html()),  # solver gets the full article
    )
    _patch_async_client(monkeypatch, transport)
    registry = (SourceFallback("operator:teaser.test", ("teaser.test",), "flaresolverr", "", 3000),)
    result = await extraction.extract("https://teaser.test/post", get_settings(), registry)
    assert "real article body" in result.markdown


async def test_extract_flaresolverr_rule_forwards_cookies_to_solver(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.source_fallbacks import SourceFallback

    captured: dict = {}
    pages = iter([_ok_response("Access Denied"), _flaresolverr_ok(_gated_article_html())])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "flaresolverr":  # the solver POST, not the Firecrawl scrape
            captured.update(json.loads(request.content))
        return next(pages)

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))
    registry = (
        SourceFallback("op", ("gated.test",), "flaresolverr", "", 500, cookies="sid=abc; t=1"),
    )
    await extraction.extract("https://gated.test/post", get_settings(), registry)
    assert captured.get("cookies") == [
        {"name": "sid", "value": "abc", "domain": "gated.test"},
        {"name": "t", "value": "1", "domain": "gated.test"},
    ]


async def test_extract_sends_maxage_zero_for_a_fresh_scrape(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Firecrawl caches by URL; maxAge=0 forces a fresh fetch so reprocess/config changes
    # apply instead of returning a stale cached scrape.
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _ok_response("body " * 200)

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))
    await extraction.extract("https://example.test/article", get_settings())
    assert captured["maxAge"] == 0


# --- 0.28.0: JSON-LD articleBody teaser detection (chrome inflation) -------------


async def test_extract_jsonld_articlebody_routes_chrome_inflated_teaser(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A ruled host returns a teaser whose related-article chrome pads the markdown past
    # the 3000 floor, but the page's JSON-LD declares only a 176-char body. A0 trusts
    # the declared body, sees a teaser, and routes to the solver instead of narrating
    # the chrome. Without the fix the 3500-char chrome scrape would be returned as-is.
    from app.services.source_fallbacks import SourceFallback

    transport = _stub_transport(
        _ok_response_with_jsonld("word " * 700, "x" * 176),  # ~3500 chars, JSON-LD=176
        _flaresolverr_ok(_gated_article_html()),  # solver gets the real article
    )
    _patch_async_client(monkeypatch, transport)
    registry = (SourceFallback("op", ("gated.test",), "flaresolverr", "", 3000, cookies="sid=1"),)
    result = await extraction.extract("https://gated.test/post", get_settings(), registry)
    assert "real article body" in result.markdown


async def test_extract_jsonld_articlebody_left_alone_without_a_rule(
    env: Path, monkeypatch: pytest.MonkeyPatch, no_flaresolverr
) -> None:
    # A host with no rule keeps the plain scraped length even if its JSON-LD body is
    # short -- free sites are untouched, so a chrome-heavy but unflagged page is
    # accepted on the direct scrape (no bypass, no extra HTTP call).
    transport = _stub_transport(_ok_response_with_jsonld("word " * 700, "x" * 176))
    _patch_async_client(monkeypatch, transport)
    result = await extraction.extract("https://free.test/post", get_settings())
    assert result.markdown.startswith("word ")


# --- 0.28.0: archive engine dispatch + expired-cookie message --------------------


def _wayback_handler(firecrawl: httpx.Response):
    """A MockTransport handler: web.archive.org serves a CDX row + a full capture;
    any other host gets the given Firecrawl response."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "web.archive.org":
            if "/cdx/" in request.url.path:
                return httpx.Response(200, json=[["timestamp"], ["20260608120000"]])
            return httpx.Response(200, text=_gated_article_html())
        return firecrawl

    return handler


async def test_extract_archive_rule_pulls_from_wayback(
    env: Path, monkeypatch: pytest.MonkeyPatch, no_flaresolverr
) -> None:
    from app.services.source_fallbacks import SourceFallback

    transport = httpx.MockTransport(_wayback_handler(_ok_response("word " * 40)))
    _patch_async_client(monkeypatch, transport)
    registry = (SourceFallback("op", ("gated.test",), "archive", "", 3000),)
    result = await extraction.extract("https://gated.test/post", get_settings(), registry)
    assert "real article body" in result.markdown


async def test_extract_auto_archive_last_resort_on_hard_block(
    env: Path, monkeypatch: pytest.MonkeyPatch, no_flaresolverr
) -> None:
    # Near-empty scrape, no per-host rule: the automatic Wayback last resort recovers it.
    transport = httpx.MockTransport(_wayback_handler(_ok_response("Access Denied")))
    _patch_async_client(monkeypatch, transport)
    result = await extraction.extract("https://hardblock.test/a", get_settings())
    assert "real article body" in result.markdown


async def test_too_short_message_solver_with_cookies_suggests_expired(
    env: Path, monkeypatch: pytest.MonkeyPatch, no_archive
) -> None:
    # The solver ran with saved cookies but still got nothing -> the message points at
    # expired/invalid cookies rather than a generic "needs a login".
    from app.services.source_fallbacks import SourceFallback

    transport = _stub_transport(_ok_response("Access Denied"), _flaresolverr_ok(""))
    _patch_async_client(monkeypatch, transport)
    registry = (SourceFallback("op", ("gated.test",), "flaresolverr", "", 3000, cookies="sid=abc"),)
    with pytest.raises(extraction.ExtractionTooShortError, match="expired or invalid"):
        await extraction.extract("https://gated.test/a", get_settings(), registry=registry)
