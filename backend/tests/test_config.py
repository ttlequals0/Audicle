from __future__ import annotations

from pathlib import Path

import pytest
from app.config import get_settings


def test_settings_load_with_valid_env(env: Path) -> None:
    settings = get_settings()
    assert settings.BASE_URL == "https://audifeed.example.test"
    assert env == settings.DATA_DIR
    assert settings.LLM_PROVIDER == "openai-compatible"
    assert settings.LOG_FORMAT == "text"


def test_boots_unconfigured_with_compose_friendly_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The app must instantiate with nothing set: every operational field has
    a default so startup never blocks on missing config (set at runtime via
    the Settings UI)."""

    monkeypatch.chdir(tmp_path)
    for key in (
        "BASE_URL", "UI_BASE_URL", "FIRECRAWL_URL", "TTS_URL", "LLM_PROVIDER",
        "LLM_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "FEED_TITLE", "FEED_DESCRIPTION", "FEED_AUTHOR", "FEED_EMAIL",
        "FEED_ARTWORK_URL", "DATA_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.BASE_URL == "http://localhost:8000"
    assert settings.FIRECRAWL_URL == "http://firecrawl:3002"
    assert settings.TTS_URL == "http://tts-wrapper:8000"
    assert settings.LLM_PROVIDER == "openai-compatible"
    assert settings.LLM_MODEL == ""
    assert settings.OPENAI_API_KEY is None


def test_unknown_env_var_is_ignored_not_fatal(
    monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    """A leftover/legacy env var (e.g. a dropped auth key) must not crash
    startup -- extra='ignore'."""

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    get_settings.cache_clear()
    settings = get_settings()
    assert not hasattr(settings, "AUTH_ENABLED")


def test_session_cookie_secure_defaults_true(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    get_settings.cache_clear()
    assert get_settings().SESSION_COOKIE_SECURE is True


def test_cors_origin_list_splits_and_trims(monkeypatch: pytest.MonkeyPatch, env: Path) -> None:
    monkeypatch.setenv("CORS_ORIGINS", " https://a.test , https://b.test ,")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.cors_origin_list == ["https://a.test", "https://b.test"]


def test_default_artwork_url_points_at_branding_jpg():
    from app.config import Settings

    s = Settings()
    assert s.DEFAULT_ARTWORK_URL == (
        "https://raw.githubusercontent.com/ttlequals0/Audicle/main/"
        "branding/podcast-artwork-3000.jpg"
    )
