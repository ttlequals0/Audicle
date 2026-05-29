"""Application configuration via Pydantic Settings.

Resolution chain: code default -> env var -> runtime_settings DB row.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # ignore (not forbid): nothing should block startup -- a leftover or
        # legacy env var (or a deprecated auth key) is ignored rather than
        # fatal. Operational config is set at runtime via the Settings UI.
        extra="ignore",
    )

    # Everything has a default so the app boots unconfigured; the operator fills
    # these in via the Settings UI (most are in the runtime_settings allowlist).
    # Compose-friendly defaults point at the bundled service names.
    BASE_URL: str = "http://localhost:8000"
    UI_BASE_URL: str = ""
    DATA_DIR: Path = Path("/data")
    FIRECRAWL_URL: str = "http://firecrawl:3002"
    TTS_URL: str = "http://tts-wrapper:8000"
    LLM_PROVIDER: Literal["openai-compatible", "anthropic", "openrouter", "ollama"] = (
        "openai-compatible"
    )
    LLM_MODEL: str = ""
    FEED_TITLE: str = "Audicle"
    FEED_DESCRIPTION: str = ""
    FEED_AUTHOR: str = ""
    FEED_EMAIL: str = ""
    FEED_ARTWORK_URL: str = ""

    # LLM provider connection (UI-settable). Missing values surface at job time
    # and in /health/ready rather than blocking startup.
    OPENAI_BASE_URL: str | None = None
    OPENAI_API_KEY: str | None = None
    ANTHROPIC_API_KEY: str | None = None
    # OpenRouter: fixed base URL (set in services/llm.py); only the key is tunable.
    OPENROUTER_API_KEY: str | None = None
    # Ollama: openai-compatible against a local Ollama daemon. Its own base URL
    # so it can be selected without clobbering OPENAI_BASE_URL.
    OLLAMA_BASE_URL: str = "http://host.docker.internal:11434/v1"

    # Feed metadata defaults.
    FEED_LANGUAGE: str = "en-us"
    FEED_CATEGORY: str = "News"
    FEED_EXPLICIT: bool = False

    # LLM tunables.
    LLM_TEMPERATURE: float = 0.7
    LLM_MAX_TOKENS: int = 4000
    LLM_TIMEOUT_SECONDS: int = 300
    LLM_RETRY_COUNT: int = 3

    # Extraction tunables.
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

    # Auth is set up at runtime via the UI (MinusPod-style): the admin password
    # bcrypt hash lives in the settings DB table, not env. No password set =
    # open convenience mode. The session secret is auto-generated and persisted
    # to the DB; SESSION_SECRET_KEY is an optional override.
    SESSION_SECRET_KEY: str | None = None
    # ``True`` requires HTTPS for the session cookie. Default True (secure by
    # default); set false only for plain-http localhost dev.
    SESSION_COOKIE_SECURE: bool = True
    SESSION_COOKIE_MAX_AGE_SECONDS: int = Field(default=86400 * 14, ge=60)
    LOCKOUT_MAX_FAILED_ATTEMPTS: int = Field(default=5, ge=1, le=100)
    LOCKOUT_WINDOW_SECONDS: int = Field(default=15 * 60, ge=10)
    LOGIN_RATE_LIMIT: str = "10/minute"

    # Cleanup prompt + pronunciation corrections.
    MAX_PROMPT_LENGTH_BYTES: int = 10240
    MAX_CORRECTIONS_ENTRIES: int = 500

    # TTS wrapper.
    TTS_LANGUAGE: str = "en"
    TTS_DEVICE: Literal["cuda", "cpu"] = "cuda"
    TTS_HTTP_TIMEOUT_SECONDS: float = 120
    # Used by the per-chunk pipeline call site; defined here so
    # operators can tune .env now without a follow-up rebuild.
    TTS_RETRY_COUNT: int = 3
    TTS_REACHABILITY_GRACE_SECONDS: float = 60
    TTS_REACHABILITY_PROBE_TIMEOUT: float = 10
    XTTS_TEMPERATURE: float = 0.65
    XTTS_LENGTH_PENALTY: float = 1.0
    XTTS_REPETITION_PENALTY: float = 2.0
    XTTS_TOP_K: int = 50
    XTTS_TOP_P: float = 0.85

    # Chunking.
    TTS_CHUNK_TARGET_WORDS: int = 180
    TTS_CHUNK_MAX_WORDS: int = 220
    TTS_CHUNK_MAX_CHARS: int = 1100
    TTS_CHUNK_SILENCE_MS: int = 250

    # Audio pipeline.
    AUDIO_SILENCE_THRESHOLD: float = 0.003
    AUDIO_SILENCE_BUFFER_MS: int = 5
    LOUDNORM_TARGET_LUFS: float = -14
    LOUDNORM_TRUE_PEAK_DB: float = -3
    LOUDNORM_LRA: float = 7
    MP3_BITRATE: str = "128k"
    MP3_SAMPLE_RATE: int = 24000
    MP3_CHANNELS: int = 2

    # Artwork.
    ARTWORK_SIZE_PX: int = 3000
    ARTWORK_JPG_QUALITY: int = 85
    ARTWORK_FETCH_TIMEOUT_SECONDS: float = 15
    ARTWORK_MIN_SOURCE_PX: int = 600
    # Cap on the og:image download size so an attacker-controlled URL can't
    # OOM the worker by streaming a multi-GB body within the fetch timeout.
    ARTWORK_MAX_DOWNLOAD_BYTES: int = 25 * 1024 * 1024

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton. Reset via get_settings.cache_clear() in tests."""

    return Settings()  # type: ignore[call-arg]
