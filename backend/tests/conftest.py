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
    get_settings.cache_clear()
    try:
        yield data_dir
    finally:
        get_settings.cache_clear()
