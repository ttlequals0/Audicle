"""Application configuration via Pydantic Settings.

Resolution chain: code default -> env var -> runtime_settings DB row.
"""

from __future__ import annotations

import contextlib
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Hosts that ship with the "render" Site-override strategy (the headful render
# sidecar that clicks expand-to-read gates). This is the single place to maintain
# the shipped defaults -- add a host here as more sites are found to need render.
# Operators can also add their own render hosts in the Site-overrides UI, which
# override these on a host collision.
RENDER_BUILTIN_HOSTS: tuple[str, ...] = ("inc.com",)


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
    # Optional bearer token for a Firecrawl instance behind auth (e.g. the hosted
    # API or a self-hosted deployment with FIRECRAWL_API_KEY set). Empty = no auth
    # header, preserving the open self-hosted default.
    FIRECRAWL_API_KEY: str = ""
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
    # Branded default cover used when neither a per-episode og:image nor the
    # operator's FEED_ARTWORK_URL is set. Served from the repo's raw-GitHub URL
    # (extension-clean .jpg) so podcast apps cache a stable cover across deploys
    # rather than the server-local /media/default.jpg.
    DEFAULT_ARTWORK_URL: str = (
        "https://raw.githubusercontent.com/ttlequals0/Audicle/main/"
        "branding/podcast-artwork-3000.jpg"
    )

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
    # Per-call output cap. The cleanup stage processes the article in windows of
    # LLM_CLEANUP_WINDOW_CHARS, so this only has to cover one window's cleaned
    # output (a ~12K-char window cleans to <12K chars ~= <4K tokens); 16000
    # leaves generous headroom and stops the old 4000 cap from truncating.
    LLM_MAX_TOKENS: int = 16000
    # Cleanup window size (chars). Long articles are split into windows on
    # paragraph boundaries and each window is cleaned in its own LLM call, then
    # concatenated -- so article length is never bottlenecked by the output cap.
    LLM_CLEANUP_WINDOW_CHARS: int = 12000
    LLM_TIMEOUT_SECONDS: int = 300
    LLM_RETRY_COUNT: int = 3

    # Episode webhooks (0.31.0). Fire-and-forget POST to this URL on every terminal
    # job transition (episode.processed / episode.failed). Empty disables. A dead or
    # slow receiver never fails a job. Operator-tunable.
    WEBHOOK_URL: str = ""
    WEBHOOK_TIMEOUT_SECONDS: float = 10.0

    # Extraction tunables.
    # Primary extraction engine. "direct" fetches the page in-process and parses it
    # with trafilatura -- no extra service, so a fresh deploy works out of the box.
    # "firecrawl" uses a self-hosted Firecrawl container (set FIRECRAWL_URL). Either
    # way, FlareSolverr + the web archive remain the fallbacks for JS/Cloudflare pages.
    EXTRACTION_ENGINE: Literal["direct", "firecrawl"] = "direct"
    # Per-request timeout for the in-process direct fetch (seconds).
    EXTRACTION_DIRECT_TIMEOUT_SECONDS: int = 30
    # Override the User-Agent the direct engine sends; empty uses a built-in Chrome UA.
    EXTRACTION_DIRECT_USER_AGENT: str = ""
    FIRECRAWL_RETRY_COUNT: int = 3
    # Arc XP / Fusion static body extractor (0.31.0). When on, the direct scrape also
    # requests rawHtml so the Arc parser can pull the article body out of the page's
    # content_elements JSON before the browser/archive fallbacks run.
    EXTRACTION_ARC_ENABLED: bool = True
    FIRECRAWL_BACKOFF_BASE_SECONDS: int = 1
    FIRECRAWL_TIMEOUT_SECONDS: int = 30
    MIN_EXTRACTION_CHARS: int = 500
    MIN_CLEANUP_CHARS: int = 200
    # When a direct scrape of a known paywall/JS-gated host (see source_fallbacks)
    # comes back below that source's bar, retry via a reader-proxy rewrite (e.g.
    # Medium -> Freedium). False disables fallbacks (direct scrapes only).
    EXTRACTION_FALLBACKS_ENABLED: bool = True
    # FlareSolverr endpoint for the "flaresolverr" bypass strategy (a Cloudflare/
    # JS-challenge solver). Include the /v1 path (the client appends it if missing).
    # Empty disables the strategy (a matched host using it fails cleanly). Operators
    # point this at their own FlareSolverr; Audicle does not bundle one.
    FLARESOLVERR_URL: str = "http://flaresolverr:8191/v1"
    FLARESOLVERR_MAX_TIMEOUT_MS: int = 60000  # solver's own per-request browser budget
    # Render sidecar: a headful stealth browser that clicks "EXPAND TO CONTINUE
    # READING"-style controls to recover the full article body (e.g. inc.com). It runs
    # post-cascade for a host whose Site-override rule is the "render" strategy (or any
    # solved page that looks truncated) -- as enrichment on a partial and as a rescue
    # when the cascade fails. Empty RENDER_URL disables it. Builtin render hosts live in
    # RENDER_BUILTIN_HOSTS (below).
    RENDER_URL: str = ""
    RENDER_TIMEOUT_SECONDS: float = 90.0
    # Archive fallback: when a scrape is near-empty (a hard block) and no other bypass
    # recovered the article, try a Wayback Machine capture before failing. No cookies,
    # no bot wall; archive.today (via FlareSolverr) is opt-in per host, not automatic.
    ARCHIVE_FALLBACK_ENABLED: bool = True
    WAYBACK_TIMEOUT_SECONDS: int = 30
    # Firecrawl scrape filtering so chrome (nav, cookie banners, footers) is
    # dropped before the LLM ever sees it. onlyMainContent is Firecrawl's
    # main-article heuristic; excludeTags drops elements by tag/selector.
    FIRECRAWL_ONLY_MAIN_CONTENT: bool = True
    FIRECRAWL_REMOVE_BASE64_IMAGES: bool = True
    # Comma-separated like CORS_ORIGINS (pydantic-settings JSON-parses list env
    # vars, which makes a plain comma list crash startup); split via the
    # firecrawl_exclude_tags property.
    FIRECRAWL_EXCLUDE_TAGS: str = "nav,footer,header,aside"

    # Queue / HTTP / worker.
    QUEUE_POLL_INTERVAL_SECONDS: float = 2.0
    # Base per-job ceiling. The effective timeout scales with the chunk count
    # (see pipeline.effective_job_timeout): a long document gets proportionally
    # more time. Both this and the per-chunk budget are operator-tunable so a
    # slower GPU/CPU can be accommodated without a redeploy.
    JOB_TIMEOUT_SECONDS: float = Field(default=3600, gt=0)
    # Per-TTS-chunk time budget used to scale the per-job timeout once the chunk
    # count is known: effective = max(JOB_TIMEOUT_SECONDS, chunks * this).
    # ~15.8 s/chunk was observed on a GPU; 30 s leaves headroom for slow or
    # regenerated chunks and slower hardware.
    JOB_TIMEOUT_PER_CHUNK_SECONDS: float = Field(default=30.0, gt=0)
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
    # When true, derive the client IP for the login rate-limit and lockout from
    # X-Forwarded-For -- the entry TRUSTED_PROXY_HOPS from the right (the hop your
    # own proxy appended), not the client-controlled leftmost value. Leave false
    # unless Audicle sits behind a trusted proxy: trusting the header when nothing
    # strips it lets a client spoof its IP and evade the limit/lockout.
    TRUST_PROXY_HEADERS: bool = False
    TRUSTED_PROXY_HOPS: int = Field(default=1, ge=1, le=10)

    # Cleanup prompt + pronunciation corrections.
    MAX_PROMPT_LENGTH_BYTES: int = 10240
    MAX_CORRECTIONS_ENTRIES: int = 500
    # When true, the full base lexicon is applied to every matching token
    # (aggressive). Only high-confidence base respellings are applied so
    # auto-derived entries can't regress words the engine already says correctly.
    LEXICON_AGGRESSIVE: bool = True

    # TTS wrapper.
    TTS_LANGUAGE: str = "en"
    TTS_DEVICE: Literal["cuda", "cpu"] = "cuda"
    TTS_HTTP_TIMEOUT_SECONDS: float = 120
    # Used by the per-chunk pipeline call site; defined here so
    # operators can tune .env now without a follow-up rebuild.
    TTS_RETRY_COUNT: int = 3
    TTS_REACHABILITY_GRACE_SECONDS: float = 60
    TTS_REACHABILITY_PROBE_TIMEOUT: float = 10

    # Chunking.
    # Chunk size = transcript-cue granularity + per-chunk TTS round-trips. The
    # tts-wrapper splits each chunk into engine-safe sentence pieces internally, so
    # chunks do NOT need to be sentence-sized; ~120 words keeps the cue/round-trip
    # count sane (a long article is ~30 chunks, not ~160). MAX stays generous so a
    # long single sentence doesn't trip UnsplittableSentenceError.
    TTS_CHUNK_TARGET_WORDS: int = 120
    TTS_CHUNK_MAX_WORDS: int = 220
    TTS_CHUNK_MAX_CHARS: int = 1100
    TTS_CHUNK_SILENCE_MS: int = 250

    # Audio pipeline.
    # Append an operator-uploaded chime clip to the end of every episode (so back-to-back
    # episodes are distinguishable). Off unless enabled AND a clip has been uploaded.
    CHIME_ENABLED: bool = False
    AUDIO_SILENCE_THRESHOLD: float = 0.003
    AUDIO_SILENCE_BUFFER_MS: int = 5
    LOUDNORM_TARGET_LUFS: float = -14
    LOUDNORM_TRUE_PEAK_DB: float = -3
    LOUDNORM_LRA: float = 7
    MP3_BITRATE: str = "128k"
    MP3_SAMPLE_RATE: int = 24000
    MP3_CHANNELS: int = 2

    # Post-TTS audio quality analysis: detect a chunk that came back as a flat
    # drone / steady noise / repetition and regenerate it (Chatterbox is
    # non-deterministic, so a re-gen usually fixes it). Thresholds are starting
    # points and need empirical tuning against real failures.
    AUDIO_ANALYSIS_ENABLED: bool = True
    AUDIO_ANALYSIS_MAX_REGEN: int = 2  # extra attempts on a bad chunk
    AUDIO_ANALYSIS_FRAME_MS: int = 25
    AUDIO_ANALYSIS_HOP_MS: int = 10
    AUDIO_ANALYSIS_MIN_RMS_CV: float = 0.35  # below = flat envelope (drone/noise)
    AUDIO_ANALYSIS_MIN_CREST: float = 3.0  # below = non-peaky (tone), linear ratio
    AUDIO_ANALYSIS_MAX_ZCR: float = 0.35  # above = broadband noise (with low rms_cv)
    AUDIO_ANALYSIS_MAX_SILENT_FRACTION: float = 0.85
    AUDIO_ANALYSIS_WORDS_PER_SEC: float = 2.7
    # Fixed per-chunk cost (inter-piece silence + a single word floor) added to
    # the word-count estimate, so a 1-2 word chunk isn't a false "overlong".
    AUDIO_ANALYSIS_DURATION_OVERHEAD_SECS: float = 1.0
    AUDIO_ANALYSIS_MAX_DURATION_RATIO: float = 2.0  # over-long => repetition
    AUDIO_ANALYSIS_MIN_DURATION_RATIO: float = 0.25  # too-short => truncation

    # Post-TTS ASR verification (defense-in-depth, off by default). When enabled,
    # the wrapper transcribes each produced chunk with faster-whisper and the
    # backend diffs that transcript against the expected narration text; a high
    # word-level divergence (dropout, hallucination, leaked preamble) is treated
    # as a quality failure and regenerated on the same AUDIO_ANALYSIS_MAX_REGEN
    # loop. The wrapper must also have WHISPER_ENABLED=true to load the model.
    WHISPER_VERIFY_ENABLED: bool = False
    # 0..1; above this = regenerate. 0.35 rather than a tighter value because ASR
    # mishears technical jargon (execve, hex, paths) on code-heavy articles, which
    # inflates divergence even when the audio is fine; this catches gross dropout
    # and hallucination without over-regenerating. Tune live via Settings.
    WHISPER_DIVERGENCE_THRESHOLD: float = 0.35
    WHISPER_VERIFY_MIN_WORDS: int = 8  # skip tiny chunks where ASR noise dominates

    # Artwork.
    ARTWORK_SIZE_PX: int = 3000
    ARTWORK_JPG_QUALITY: int = 85
    ARTWORK_FETCH_TIMEOUT_SECONDS: float = 15
    ARTWORK_MIN_SOURCE_PX: int = 600
    # Cap on the og:image download size so an attacker-controlled URL can't
    # OOM the worker by streaming a multi-GB body within the fetch timeout.
    ARTWORK_MAX_DOWNLOAD_BYTES: int = 25 * 1024 * 1024

    # Direct file upload. Per-file ceiling for the /upload endpoint, in MEGABYTES
    # (0.31.0; was UPLOAD_MAX_BYTES). The stream is aborted mid-read once it crosses
    # the cap so a hostile payload can't fully buffer first. Operator-tunable
    # (runtime_settings) for image-heavy PDFs; UI + API both speak MB.
    UPLOAD_MAX_MB: int = Field(default=50, ge=1)

    @model_validator(mode="after")
    def _legacy_upload_max_bytes(self) -> Settings:
        """Back-compat for the 0.31.0 UPLOAD_MAX_BYTES -> UPLOAD_MAX_MB rename.

        A pre-0.31.0 deployment may set ``UPLOAD_MAX_BYTES`` (bytes) in its env. That
        var is no longer a field, so read it directly: when the operator did not set
        the new ``UPLOAD_MAX_MB`` explicitly, derive MB from the legacy bytes value so
        their cap survives the upgrade. (DB-stored overrides are handled by migration
        015.)
        """

        if "UPLOAD_MAX_MB" not in self.model_fields_set:
            legacy = os.environ.get("UPLOAD_MAX_BYTES")
            if legacy:
                with contextlib.suppress(ValueError):
                    self.UPLOAD_MAX_MB = max(1, int(legacy) // (1024 * 1024))
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def firecrawl_exclude_tags(self) -> list[str]:
        return [t.strip() for t in self.FIRECRAWL_EXCLUDE_TAGS.split(",") if t.strip()]

    @property
    def firecrawl_configured(self) -> bool:
        """True when Firecrawl is actually reachable, i.e. FIRECRAWL_URL was set to a
        real instance. The compose default placeholder counts as not configured, so a
        direct-engine deploy never tries the Firecrawl re-scrape fallbacks against a
        host that isn't there."""

        url = self.FIRECRAWL_URL.strip()
        return bool(url) and url != "http://firecrawl:3002"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton. Reset via get_settings.cache_clear() in tests."""

    return Settings()  # type: ignore[call-arg]
