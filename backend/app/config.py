"""Application configuration via Pydantic Settings.

Resolution chain: code default -> env var -> runtime_settings DB row (added in Phase 10).
Phase 1 covers code defaults and env vars only.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="forbid",
    )

    # Required (no defaults) -- fail fast at startup if missing.
    BASE_URL: str = Field(description="Public feed URL base (https://audifeed.example.com)")
    UI_BASE_URL: str = Field(description="Admin UI URL base")
    DATA_DIR: Path = Field(description="Path that holds podcast.db, media/, backups, locks")
    FIRECRAWL_URL: str = Field(description="HTTP base for the Firecrawl instance")
    TTS_URL: str = Field(description="HTTP base for the XTTS wrapper")
    LLM_PROVIDER: Literal["openai-compatible", "anthropic"] = Field(
        description="Which LLM backend the cleanup stage uses"
    )
    LLM_MODEL: str = Field(description="Model identifier (example: qwen2.5:14b)")
    FEED_TITLE: str = Field(description="Channel title for the RSS feed")
    FEED_DESCRIPTION: str = Field(description="Channel description for the RSS feed")
    FEED_AUTHOR: str = Field(description="iTunes author")
    FEED_EMAIL: str = Field(description="iTunes owner email")
    FEED_ARTWORK_URL: str = Field(description="Default channel artwork URL")

    # Conditionally required (validated below).
    OPENAI_BASE_URL: str | None = None
    OPENAI_API_KEY: str | None = None
    ANTHROPIC_API_KEY: str | None = None

    # Feed metadata defaults.
    FEED_LANGUAGE: str = "en-us"
    FEED_CATEGORY: str = "News"
    FEED_EXPLICIT: bool = False

    # LLM tunables (used from Phase 3 onward; defined here for completeness).
    LLM_TEMPERATURE: float = 0.7
    LLM_MAX_TOKENS: int = 4000
    LLM_TIMEOUT_SECONDS: int = 300
    LLM_RETRY_COUNT: int = 3

    # Extraction tunables (Phase 2).
    FIRECRAWL_RETRY_COUNT: int = 3
    FIRECRAWL_BACKOFF_BASE_SECONDS: int = 1
    FIRECRAWL_TIMEOUT_SECONDS: int = 30
    MIN_EXTRACTION_CHARS: int = 500
    MIN_CLEANUP_CHARS: int = 200

    # Queue / HTTP / worker.
    QUEUE_POLL_INTERVAL_SECONDS: float = 2.0
    JOB_TIMEOUT_SECONDS: float = 1800
    WEB_WORKERS: int = 2

    # Logging.
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_FORMAT: Literal["json", "text"] = "json"

    # Retention.
    RETENTION_DAYS: int = 90
    RETENTION_SWEEP_HOUR_UTC: int = 7
    MIGRATION_BACKUP_RETENTION_DAYS: int = 30

    # RSS.
    RSS_CACHE_MAX_AGE_SECONDS: int = 300

    # CORS.
    CORS_ORIGINS: str = ""

    @model_validator(mode="after")
    def _validate_provider(self) -> Settings:
        if self.LLM_PROVIDER == "openai-compatible":
            missing: list[str] = []
            if not self.OPENAI_BASE_URL:
                missing.append("OPENAI_BASE_URL")
            if not self.OPENAI_API_KEY:
                missing.append("OPENAI_API_KEY")
            if missing:
                raise ValueError(f"LLM_PROVIDER=openai-compatible requires {', '.join(missing)}")
        elif self.LLM_PROVIDER == "anthropic":
            if not self.ANTHROPIC_API_KEY:
                raise ValueError("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton. Reset via get_settings.cache_clear() in tests."""

    return Settings()  # type: ignore[call-arg]
