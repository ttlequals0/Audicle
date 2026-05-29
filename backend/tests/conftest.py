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


@pytest.fixture(autouse=True)
def _isolate_editable_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Redirect the prompt + corrections file resolvers into tmp_path.

    Their production paths resolve to the source tree (the bind mount points
    host edits and API edits at the same shipped files). Without this, any
    test that PUTs a prompt/corrections payload rewrites the real
    ``backend/app/prompts/script.txt`` / ``corrections/pronunciation.json`` --
    which once silently clobbered the shipped default prompt. Seed minimal
    valid defaults so GET-style tests still see non-empty content.
    """

    pdir = tmp_path / "prompts"
    cdir = tmp_path / "corrections"
    pdir.mkdir(exist_ok=True)
    cdir.mkdir(exist_ok=True)
    prompt_file = pdir / "script.txt"
    corrections_file = cdir / "pronunciation.json"
    prompt_file.write_text("Test cleanup prompt rules.\n", encoding="utf-8")
    corrections_file.write_text("{}", encoding="utf-8")

    import app.api.v1.corrections as corr_api
    import app.api.v1.prompt as prompt_api
    import app.services.pipeline as pipeline_mod

    monkeypatch.setattr(prompt_api, "_prompt_path", lambda: prompt_file)
    monkeypatch.setattr(corr_api, "_corrections_path", lambda: corrections_file)
    monkeypatch.setattr(pipeline_mod, "_prompt_path", lambda _settings: prompt_file)
    monkeypatch.setattr(pipeline_mod, "_corrections_path", lambda _settings: corrections_file)
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
    # Reachability checks in tests must finish fast; tests that need a longer
    # grace can override these in their own monkeypatch.
    monkeypatch.setenv("TTS_REACHABILITY_GRACE_SECONDS", "0.5")
    monkeypatch.setenv("TTS_REACHABILITY_PROBE_TIMEOUT", "0.5")
    get_settings.cache_clear()
    try:
        yield data_dir
    finally:
        get_settings.cache_clear()
