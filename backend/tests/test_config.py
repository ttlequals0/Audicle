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


def test_openai_compatible_requires_base_url_and_key(
    monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    from pydantic import ValidationError

    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="OPENAI_BASE_URL"):
        get_settings()


def test_anthropic_requires_api_key(monkeypatch: pytest.MonkeyPatch, env: Path) -> None:
    from pydantic import ValidationError

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        get_settings()


def test_missing_required_field_raises(monkeypatch: pytest.MonkeyPatch, env: Path) -> None:
    from pydantic import ValidationError

    monkeypatch.delenv("BASE_URL", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()


def test_cors_origin_list_splits_and_trims(monkeypatch: pytest.MonkeyPatch, env: Path) -> None:
    monkeypatch.setenv("CORS_ORIGINS", " https://a.test , https://b.test ,")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.cors_origin_list == ["https://a.test", "https://b.test"]
