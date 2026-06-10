"""Shared pytest fixtures.

Sets a minimal valid env for the Pydantic Settings model so individual tests
don't have to repeat the boilerplate. Tests that need to vary a specific value
override it with monkeypatch and call ``get_settings.cache_clear()``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import get_settings

_REQUIRED = {
    "BASE_URL": "https://audifeed.example.test",
    "UI_BASE_URL": "https://audicle.example.test",
    "FIRECRAWL_URL": "http://firecrawl.test:3002",
    "TTS_URL": "http://tts.test:8000",
    "LLM_PROVIDER": "openai-compatible",
    "LLM_MODEL": "qwen-test",
    "OPENAI_BASE_URL": "http://llm.test/v1",
    "OPENAI_API_KEY": "test-key",
    "FEED_TITLE": "Test Feed",
    "FEED_DESCRIPTION": "Test description",
    "FEED_AUTHOR": "Test Author",
    "FEED_EMAIL": "test@example.test",
    "FEED_ARTWORK_URL": "https://audifeed.example.test/static/art.png",
}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_ssrf: run with the real SSRF resolver instead of the public-IP stub",
    )


@pytest.fixture(autouse=True)
def _stub_ssrf_resolver(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests submit placeholder URLs (``example.test``) that don't resolve;
    the SSRF guard on /submit and in extraction would reject them on DNS failure.
    Stub the resolver to a public IP so the guard passes through. Tests that
    exercise the guard itself opt out with the ``real_ssrf`` marker."""

    if request.node.get_closest_marker("real_ssrf"):
        return

    from app.services import ssrf

    async def _resolve_public(_host: str) -> str:
        return "203.0.113.1"

    monkeypatch.setattr(ssrf, "resolve_public_host", _resolve_public)


@pytest.fixture(autouse=True)
def _reset_login_rate_limiter():
    """The slowapi limiter on the login route is a module-level singleton;
    its in-memory store accumulates hits across tests and eventually 429s.
    Reset between each test so per-test counters start at zero."""

    try:
        from app.api.v1.auth import _LOGIN_LIMITER

        _LOGIN_LIMITER.reset()
    except ImportError:
        pass
    yield


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Stand up a minimal valid env in tmp_path and reset the settings cache.

    Also chdir into tmp_path so pydantic-settings doesn't pick up a real .env
    sitting in the repo root. Clears the lru_cache on teardown so the next
    test's Settings is rebuilt against its own monkeypatched env.
    """

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    for key, value in _REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("LOG_FORMAT", "text")
    # Don't import the ~400k-row bundled base lexicon on every app/worker startup
    # in tests; the migration's seed layer suffices and test_lexicon covers sync.
    monkeypatch.setenv("AUDICLE_SKIP_LEXICON_SYNC", "1")
    # TestClient speaks plain http; the production default (secure cookies)
    # would drop the session cookie so login wouldn't persist in tests.
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    # Reachability checks in tests must finish fast; tests that need a longer
    # grace can override these in their own monkeypatch.
    monkeypatch.setenv("TTS_REACHABILITY_GRACE_SECONDS", "0.5")
    monkeypatch.setenv("TTS_REACHABILITY_PROBE_TIMEOUT", "0.5")
    get_settings.cache_clear()
    try:
        yield data_dir
    finally:
        get_settings.cache_clear()
