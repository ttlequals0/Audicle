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

    # Authenticated feeds (optional, off by default). When enabled, every public
    # feed/media URL requires FEED_AUTH_KEY (a 64-hex key generated on enable);
    # requests without it get 401. The key is not masked in the settings API --
    # the operator must read it to build the subscribe URL. Managed via the
    # /api/v1/feed-auth endpoints and the Settings widget, not the generic form.
    FEED_AUTH_ENABLED: bool = False
    FEED_AUTH_KEY: str = ""

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
    # Reader-proxy ("reader" strategy) endpoint. Jina Reader returns clean markdown and
    # bypasses DataDome/PerimeterX bot walls (e.g. wsj.com). Must contain {url} and resolve
    # to a public address (the fetch is SSRF-guarded, like the direct engine).
    READER_PROXY_TEMPLATE: str = "https://r.jina.ai/{url}"
    # Optional Jina API key (sent as a Bearer token), free at https://jina.ai/reader. The
    # keyless public endpoint is rate limited; a key raises the limit substantially -- the
    # simplest fix if the reader strategy starts returning empty/truncated bodies. Settable
    # live from the Settings UI (Connections) or PUT /api/v1/settings. Empty sends no auth.
    READER_API_KEY: str = ""
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
    # A job is killed when it STALLS, not when it has simply run a long time
    # (see pipeline._Watchdog). These two size the absolute ceiling the watchdog
    # falls back on: estimate = max(JOB_TIMEOUT_SECONDS, chunks * per-chunk).
    # Both stay operator-tunable so slower hardware can be accommodated without
    # a redeploy.
    JOB_TIMEOUT_SECONDS: float = Field(default=3600, gt=0)
    # Per-TTS-chunk budget folded into that estimate. ~15.8 s/chunk was observed
    # on a GPU; 30 s leaves headroom for slow or regenerated chunks.
    JOB_TIMEOUT_PER_CHUNK_SECONDS: float = Field(default=30.0, gt=0)
    # No progress heartbeat (a stage change or a chunk completing) for this long
    # means the job is dead. Floor is set by the worst legitimate silence: one
    # /generate call can burn TTS_RETRY_COUNT x TTS_HTTP_TIMEOUT_SECONDS = 840 s
    # against a hung wrapper, so 1800 s leaves better than 2x margin.
    JOB_STALL_SECONDS: float = Field(default=1800, gt=0)
    # Absolute ceiling as a multiple of the chunk-scaled estimate, so a job that
    # keeps inching forward forever still cannot hold the single worker. 3x of a
    # 195-chunk estimate is ~4.9 h.
    JOB_TIMEOUT_CEILING_MULTIPLIER: float = Field(default=3.0, ge=1.0)
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
    # 7 attempts = ~90s cumulative backoff, enough to ride out the wrapper's
    # ~40s restart + model reload after an OOM kill (3 attempts was ~6s, so
    # any wrapper restart failed the whole episode). Trade-off: a wrapper that
    # HANGS instead of dying now takes up to 7 x TTS_HTTP_TIMEOUT_SECONDS
    # (~15 min) to surface a chunk failure; lower this if hang-mode failures
    # matter more than restart-riding.
    TTS_RETRY_COUNT: int = 7
    TTS_REACHABILITY_GRACE_SECONDS: float = 60
    TTS_REACHABILITY_PROBE_TIMEOUT: float = 10

    # Chatterbox generation knobs, sent to the wrapper on every /generate call
    # (the wrapper no longer reads them from its own env, 0.44.0). All are
    # runtime-tunable via Settings; a change applies to the next job with no
    # restart. Only knobs the Turbo model honors are exposed (it ignores CFG
    # and exaggeration): temperature 0.5 (below Turbo's 0.8) trims sampling
    # variance; repetition_penalty/top_p/top_k are Turbo's other sampling
    # knobs at their library defaults; seed makes a chunk reproducible (0
    # disables seeding). CHATTERBOX_MAX_CHARS caps the sentence pieces the
    # wrapper feeds the model per inference call; larger = fewer context
    # boundaries per chunk. Value ranges live in RUNTIME_SETTING_BOUNDS below
    # (as data, not Field constraints, so a stale wrapper-era env var with an
    # out-of-range value can't fail backend startup).
    CHATTERBOX_TEMPERATURE: float = 0.5
    CHATTERBOX_REPETITION_PENALTY: float = 1.2
    CHATTERBOX_TOP_P: float = 0.95
    CHATTERBOX_TOP_K: int = 1000
    CHATTERBOX_SEED: int = 1234
    CHATTERBOX_MAX_CHARS: int = 300

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
    # Mid-chunk dead air: Chatterbox sometimes generates multi-second silence
    # inside a piece, which lands mid-chunk where the edge trim can't reach.
    # Internal silence runs longer than MAX are cut down to KEEP (half kept at
    # each end of the run). MAX = 0 disables the pass.
    AUDIO_MAX_INTERNAL_SILENCE_MS: int = 1000
    AUDIO_INTERNAL_SILENCE_KEEP_MS: int = 500
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
    # Chapters (3b): one LLM call after the audio stage names 3-7 chapters;
    # emitted as podcast:chapters JSON and embedded ID3 CHAP/CTOC frames.
    # Episodes under the duration floor get none (navigation noise).
    CHAPTERS_ENABLED: bool = True
    CHAPTERS_MIN_DURATION_SECS: int = 600

    # Read "{title}. By {author}." at the top of each episode (3a). The line is
    # prepended to the cleaned text before chunking so it flows through
    # corrections, TTS, the transcript, and chapter timing with no special case.
    INTRO_READ_ENABLED: bool = True

    AUDIO_ANALYSIS_ENABLED: bool = True
    AUDIO_ANALYSIS_MAX_REGEN: int = 2  # extra attempts on a bad chunk
    AUDIO_ANALYSIS_FRAME_MS: int = 25
    AUDIO_ANALYSIS_HOP_MS: int = 10
    AUDIO_ANALYSIS_WINDOW_SECS: float = 3.0  # sliding-window size for localized checks
    # Pitch-drift gate (2b): a chunk whose median F0 deviates from the running
    # median of accepted chunks by more than this many semitones is regenerated.
    # Set by the 0.51.0 corpus sweep (149 episodes): p95 of 30s-bucket
    # deviations was 1.21 st while the pitch-complaint episode peaked at 1.35,
    # so 1.25 flags the complaint without churning on normal variation.
    AUDIO_ANALYSIS_MAX_F0_SEMITONES: float = 1.25
    AUDIO_ANALYSIS_F0_WARMUP_CHUNKS: int = 3  # accepted chunks before the gate arms
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
    # Regeneration escalation. A re-gen that only re-rolls the seed leaves the
    # model in the same failure basin; observed recovery was 43 -> 33 -> 24 bad
    # chunks over three identical-parameter attempts. Each retry instead shrinks
    # the per-inference text window (the repetition-loop trigger) and stiffens
    # the repetition penalty. attempt N: max_chars = CHATTERBOX_MAX_CHARS *
    # factor**N (floored), repetition_penalty = base + step * N.
    AUDIO_ANALYSIS_REGEN_CHARS_FACTOR: float = Field(default=0.6, gt=0, le=1.0)
    AUDIO_ANALYSIS_REGEN_MIN_CHARS: int = Field(default=150, ge=100, le=2000)
    AUDIO_ANALYSIS_REGEN_PENALTY_STEP: float = Field(default=0.15, ge=0, le=1.0)

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
    # A contiguous run of this many-or-more diverging words (dropout or babble)
    # regenerates the chunk. The global ratio above dilutes localized garbage --
    # 15 bad words inside a 120-word chunk score ~0.15 and pass, yet are plainly
    # audible. 8 clears real mangles (observed runs are 11-30+ words) while
    # staying above ASR proper-noun mishears (2-4 words). Tune live via Settings.
    WHISPER_MAX_DIVERGENT_RUN: int = 8
    # Below this comparison word count the tuned thresholds are skipped and only
    # WHISPER_SHORT_CHUNK_DIVERGENCE applies: ASR noise dominates tiny chunks.
    WHISPER_VERIFY_MIN_WORDS: int = 8
    # Gross-mismatch bar for short chunks. 0.8 tolerates whisper's deterministic
    # proper-noun mishears on tiny word lists (2 of 3 name words misheard scores
    # ~0.67) while still catching fully garbled audio (an unrelated or empty
    # transcript scores ~1.0).
    WHISPER_SHORT_CHUNK_DIVERGENCE: float = 0.8

    # Artwork.
    ARTWORK_SIZE_PX: int = 3000
    # Size of the cover embedded in each MP3 (ID3 APIC) for players that read only
    # embedded art (Pocket Casts). Smaller than the 3000px feed master to keep the
    # per-episode file-size cost down; a 1400px JPEG is ~150-350 kB.
    EMBED_ARTWORK_SIZE_PX: int = 1400
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


# Value ranges for the numeric runtime knobs, enforced by PUT /api/v1/settings
# (api/v1/settings.py) so a bad value is rejected at save time instead of
# failing every TTS call or chunker run later. Declared as data rather than
# Field constraints so a stale env var can't fail Settings() at startup. The
# CHATTERBOX_* entries mirror the wrapper's request bounds
# (tts-wrapper/engine.py GENERATION_BOUNDS); a test pins the two together.
RUNTIME_SETTING_BOUNDS: dict[str, dict[str, float]] = {
    "CHATTERBOX_TEMPERATURE": {"gt": 0, "le": 2.0},
    "CHATTERBOX_REPETITION_PENALTY": {"ge": 1.0, "le": 2.0},
    "CHATTERBOX_TOP_P": {"gt": 0, "le": 1.0},
    "CHATTERBOX_TOP_K": {"ge": 1, "le": 10000},
    "CHATTERBOX_SEED": {"ge": 0},
    "CHATTERBOX_MAX_CHARS": {"ge": 100, "le": 2000},
    # Backend chunker ceiling; the wrapper's request text cap is 4000 chars.
    "TTS_CHUNK_MAX_CHARS": {"ge": 200, "le": 4000},
    # 0 would flag every chunk (asr_run >= 0 always holds).
    "WHISPER_MAX_DIVERGENT_RUN": {"ge": 1},
    "WHISPER_SHORT_CHUNK_DIVERGENCE": {"gt": 0, "le": 1.0},
    # Watchdog: stall window and the ceiling multiplier over the chunk-scaled
    # estimate. A multiplier below 1 would put the ceiling under the estimate.
    "JOB_STALL_SECONDS": {"gt": 0},
    "JOB_TIMEOUT_CEILING_MULTIPLIER": {"ge": 1.0},
    # Regen escalation. The chars floor mirrors the wrapper's max_chars bounds
    # so a floored value is still a legal request.
    "AUDIO_ANALYSIS_REGEN_CHARS_FACTOR": {"gt": 0, "le": 1.0},
    "AUDIO_ANALYSIS_REGEN_MIN_CHARS": {"ge": 100, "le": 2000},
    "AUDIO_ANALYSIS_REGEN_PENALTY_STEP": {"ge": 0, "le": 1.0},
}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton. Reset via get_settings.cache_clear() in tests."""

    return Settings()  # type: ignore[call-arg]
