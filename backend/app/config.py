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
    RETENTION_DAYS: int = Field(default=90, ge=0, le=100_000)
    RETENTION_SWEEP_HOUR_UTC: int = Field(default=7, ge=0, le=23)
    MIGRATION_BACKUP_RETENTION_DAYS: int = Field(default=30, ge=0)

    # RSS.
    RSS_CACHE_MAX_AGE_SECONDS: int = 300

    # CORS.
    CORS_ORIGINS: str = ""

    # Auth (Phase 9). When AUTH_ENABLED is false the admin endpoints accept
    # unauthenticated requests; this is the default for single-operator
    # localhost installs. Public-internet deployments must set
    # AUTH_ENABLED=true plus an admin password.
    AUTH_ENABLED: bool = False
    ADMIN_USERNAME: str = "admin"
    # Bcrypt hash. Operator runs ``python -c "from app.services import auth;
    # print(auth.hash_password('secret'))"`` to generate. Required when
    # AUTH_ENABLED=true.
    ADMIN_PASSWORD_HASH: str | None = None
    # Cookie-signing key for SessionMiddleware. MUST be set when
    # AUTH_ENABLED=true; refuse to start otherwise. Operator generates with
    # ``python -c "import secrets; print(secrets.token_urlsafe(64))"``.
    SESSION_SECRET_KEY: str | None = None
    # ``True`` requires HTTPS for the cookie to be sent; default False so
    # localhost http:// works. Flip to True in production.
    SESSION_COOKIE_SECURE: bool = False
    SESSION_COOKIE_MAX_AGE_SECONDS: int = Field(default=86400 * 14, ge=60)
    LOCKOUT_MAX_FAILED_ATTEMPTS: int = Field(default=5, ge=1, le=100)
    LOCKOUT_WINDOW_SECONDS: int = Field(default=15 * 60, ge=10)
    LOGIN_RATE_LIMIT: str = "10/minute"

    # Cleanup prompt + pronunciation corrections.
    MAX_PROMPT_LENGTH_BYTES: int = 10240
    MAX_CORRECTIONS_ENTRIES: int = 500

    # TTS wrapper (Phase 4).
    TTS_LANGUAGE: str = "en"
    TTS_DEVICE: Literal["cuda", "cpu"] = "cuda"
    TTS_HTTP_TIMEOUT_SECONDS: float = 120
    # Wired into the per-chunk pipeline call site in Phase 5; defined here so
    # operators can tune .env now without a follow-up rebuild.
    TTS_RETRY_COUNT: int = 3
    TTS_REACHABILITY_GRACE_SECONDS: float = 60
    TTS_REACHABILITY_PROBE_TIMEOUT: float = 10
    XTTS_TEMPERATURE: float = 0.65
    XTTS_LENGTH_PENALTY: float = 1.0
    XTTS_REPETITION_PENALTY: float = 2.0
    XTTS_TOP_K: int = 50
    XTTS_TOP_P: float = 0.85

    # Chunking (Phase 5).
    TTS_CHUNK_TARGET_WORDS: int = 180
    TTS_CHUNK_MAX_WORDS: int = 220
    TTS_CHUNK_MAX_CHARS: int = 1100
    TTS_CHUNK_SILENCE_MS: int = 250

    # Audio pipeline (Phase 5).
    AUDIO_SILENCE_THRESHOLD: float = 0.003
    AUDIO_SILENCE_BUFFER_MS: int = 5
    LOUDNORM_TARGET_LUFS: float = -14
    LOUDNORM_TRUE_PEAK_DB: float = -3
    LOUDNORM_LRA: float = 7
    MP3_BITRATE: str = "128k"
    MP3_SAMPLE_RATE: int = 24000
    MP3_CHANNELS: int = 2

    # Artwork (Phase 6).
    ARTWORK_SIZE_PX: int = 3000
    ARTWORK_JPG_QUALITY: int = 85
    ARTWORK_FETCH_TIMEOUT_SECONDS: float = 15
    ARTWORK_MIN_SOURCE_PX: int = 600
    # Cap on the og:image download size so an attacker-controlled URL can't
    # OOM the worker by streaming a multi-GB body within the fetch timeout.
    ARTWORK_MAX_DOWNLOAD_BYTES: int = 25 * 1024 * 1024

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

    @model_validator(mode="after")
    def _validate_auth(self) -> Settings:
        if not self.AUTH_ENABLED:
            return self
        missing: list[str] = []
        if not self.ADMIN_PASSWORD_HASH:
            missing.append("ADMIN_PASSWORD_HASH")
        if not self.SESSION_SECRET_KEY:
            missing.append("SESSION_SECRET_KEY")
        if missing:
            raise ValueError(f"AUTH_ENABLED=true requires {', '.join(missing)}")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton. Reset via get_settings.cache_clear() in tests."""

    return Settings()  # type: ignore[call-arg]
