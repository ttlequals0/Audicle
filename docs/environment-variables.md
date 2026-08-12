# Environment variables

Every variable the backend reads, grouped by subject. "Runtime" means the key is also editable from the Settings UI (and `PUT /api/v1/settings`) with no restart; its stored value overrides the env value, and clearing it in the UI falls back to env. Env-only keys need a container restart to change.

Defaults and per-variable commentary live in `.env.example`; this page is the map. The wrapper and render sidecar have their own small env sets, covered at the end.

## Identity and feed

Who the podcast is. BASE_URL is baked into every enclosure URL, so changing it after publishing desynchronizes clients from their media.

| Variable | Default | Runtime |
|---|---|---|
| `BASE_URL` | `http://localhost:8000` | env-only |
| `UI_BASE_URL` | `` | env-only |
| `FEED_TITLE` | `Audicle` | yes |
| `FEED_DESCRIPTION` | `` | yes |
| `FEED_AUTHOR` | `` | yes |
| `FEED_EMAIL` | `` | yes |
| `FEED_LANGUAGE` | `en-us` | yes |
| `FEED_CATEGORY` | `News` | yes |
| `FEED_EXPLICIT` | `False` | yes |
| `FEED_ARTWORK_URL` | `` | yes |
| `DEFAULT_ARTWORK_URL` | `(` | env-only |
| `FEED_AUTH_ENABLED` | `False` | yes |
| `FEED_AUTH_KEY` | `` | yes |
| `RSS_CACHE_MAX_AGE_SECONDS` | `300` | yes |
| `RETENTION_DAYS` | `90` | yes |
| `RETENTION_SWEEP_HOUR_UTC` | `7` | env-only |

## LLM provider

See the LLM providers page. Keys are stored masked.

| Variable | Default | Runtime |
|---|---|---|
| `LLM_PROVIDER` | `(` | yes |
| `LLM_MODEL` | `` | yes |
| `OPENAI_BASE_URL` | `None` | yes |
| `OPENAI_API_KEY` | `None` | yes |
| `ANTHROPIC_API_KEY` | `None` | yes |
| `OPENROUTER_API_KEY` | `None` | yes |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434/v1` | yes |
| `LLM_TEMPERATURE` | `0.7` | yes |
| `LLM_MAX_TOKENS` | `16000` | yes |
| `LLM_CLEANUP_WINDOW_CHARS` | `12000` | yes |
| `LLM_TIMEOUT_SECONDS` | `300` | yes |
| `LLM_RETRY_COUNT` | `3` | yes |
| `LLM_PRONUNCIATION_CONCURRENCY` | `4` | yes |

## Extraction and paywalls

The cascade and its budgets. See the paywalls page.

| Variable | Default | Runtime |
|---|---|---|
| `EXTRACTION_ENGINE` | `direct` | yes |
| `EXTRACTION_DIRECT_TIMEOUT_SECONDS` | `30` | yes |
| `EXTRACTION_DIRECT_USER_AGENT` | `` | yes |
| `EXTRACTION_ARC_ENABLED` | `True` | yes |
| `EXTRACTION_FALLBACKS_ENABLED` | `True` | yes |
| `MIN_EXTRACTION_CHARS` | `500` | yes |
| `REGISTRATION_EMAIL` | `` | yes |
| `FIRECRAWL_URL` | `http://firecrawl:3002` | yes |
| `FIRECRAWL_API_KEY` | `` | yes |
| `FIRECRAWL_TIMEOUT_SECONDS` | `30` | yes |
| `FIRECRAWL_RETRY_COUNT` | `3` | yes |
| `FIRECRAWL_BACKOFF_BASE_SECONDS` | `1` | yes |
| `FIRECRAWL_ONLY_MAIN_CONTENT` | `True` | yes |
| `FIRECRAWL_REMOVE_BASE64_IMAGES` | `True` | yes |
| `FIRECRAWL_EXCLUDE_TAGS` | `nav,footer,header,aside` | yes |
| `FLARESOLVERR_URL` | `http://flaresolverr:8191/v1` | yes |
| `FLARESOLVERR_MAX_TIMEOUT_MS` | `60000  # solver's own per-request bro...` | yes |
| `READER_PROXY_TEMPLATE` | `https://r.jina.ai/{url}` | yes |
| `READER_API_KEY` | `` | yes |
| `RENDER_URL` | `` | yes |
| `RENDER_TIMEOUT_SECONDS` | `90.0` | yes |
| `ARCHIVE_FALLBACK_ENABLED` | `True` | yes |
| `WAYBACK_TIMEOUT_SECONDS` | `30` | yes |

## Cleanup and uploads

| Variable | Default | Runtime |
|---|---|---|
| `MIN_CLEANUP_CHARS` | `200` | yes |
| `MAX_PROMPT_LENGTH_BYTES` | `10240` | yes |
| `MAX_CORRECTIONS_ENTRIES` | `500` | yes |
| `LEXICON_AGGRESSIVE` | `True` | yes |
| `UPLOAD_MAX_MB` | `50` | yes |
| `OCR_ENABLED` | `True` | yes |
| `OCR_MAX_PAGES` | `40` | yes |
| `OCR_DPI` | `200` | yes |
| `OCR_MIN_CONFIDENCE` | `0.5` | yes |
| `OCR_LANGUAGE` | `en` | yes |

## TTS

The wrapper connection, model, chunking, generation, and the delivery budgets that ride out a wrapper restart.

| Variable | Default | Runtime |
|---|---|---|
| `TTS_URL` | `http://tts-wrapper:8000` | yes |
| `TTS_BACKEND` | `wrapper` | yes |
| `TTS_API_BASE_URL` | `` | yes |
| `TTS_API_KEY` | `` | yes |
| `TTS_API_MODEL` | `tts-1` | yes |
| `TTS_API_VOICE` | `alloy` | yes |
| `TTS_MODEL` | `` | yes |
| `TTS_LANGUAGE` | `en` | yes |
| `TTS_DEVICE` | `cuda` | env-only |
| `TTS_HTTP_TIMEOUT_SECONDS` | `120` | yes |
| `TTS_RETRY_COUNT` | `7` | yes |
| `TTS_CONNECT_RETRY_MAX_SECONDS` | `180` | yes |
| `TTS_REACHABILITY_GRACE_SECONDS` | `60` | yes |
| `TTS_REACHABILITY_PROBE_TIMEOUT` | `10` | yes |
| `TTS_CHUNK_TARGET_WORDS` | `120` | yes |
| `TTS_CHUNK_MAX_WORDS` | `220` | yes |
| `TTS_CHUNK_MAX_CHARS` | `1100` | yes |
| `TTS_CHUNK_SILENCE_MS` | `250` | yes |
| `CHATTERBOX_TEMPERATURE` | `0.5` | yes |
| `CHATTERBOX_REPETITION_PENALTY` | `1.2` | yes |
| `CHATTERBOX_TOP_P` | `0.95` | yes |
| `CHATTERBOX_TOP_K` | `1000` | yes |
| `CHATTERBOX_SEED` | `1234` | yes |
| `CHATTERBOX_MAX_CHARS` | `300` | yes |
| `CHIME_ENABLED` | `False` | yes |
| `INTRO_READ_ENABLED` | `True` | yes |

## Verification and audio analysis

The quality gates. WHISPER_BACKEND selects where ASR runs.

| Variable | Default | Runtime |
|---|---|---|
| `WHISPER_VERIFY_ENABLED` | `False` | yes |
| `WHISPER_BACKEND` | `wrapper` | yes |
| `WHISPER_API_BASE_URL` | `` | yes |
| `WHISPER_API_KEY` | `` | yes |
| `WHISPER_API_MODEL` | `whisper-1` | yes |
| `WHISPER_API_TIMEOUT_SECONDS` | `120` | yes |
| `WHISPER_DIVERGENCE_THRESHOLD` | `0.35` | yes |
| `WHISPER_MAX_DIVERGENT_RUN` | `8` | yes |
| `WHISPER_VERIFY_MIN_WORDS` | `8` | yes |
| `WHISPER_SHORT_CHUNK_DIVERGENCE` | `0.8` | yes |
| `AUDIO_ANALYSIS_ENABLED` | `True` | yes |
| `AUDIO_ANALYSIS_MAX_REGEN` | `2  # extra attempts on a bad chunk` | yes |
| `AUDIO_ANALYSIS_FRAME_MS` | `25` | yes |
| `AUDIO_ANALYSIS_HOP_MS` | `10` | yes |
| `AUDIO_ANALYSIS_WINDOW_SECS` | `3.0  # sliding-window size for locali...` | yes |
| `AUDIO_ANALYSIS_MAX_F0_SEMITONES` | `2.0` | yes |
| `AUDIO_ANALYSIS_F0_WARMUP_CHUNKS` | `3  # accepted chunks before the gate ...` | yes |
| `AUDIO_ANALYSIS_MIN_RMS_CV` | `0.35  # below = flat envelope (drone/...` | yes |
| `AUDIO_ANALYSIS_MIN_CREST` | `3.0  # below = non-peaky (tone), line...` | yes |
| `AUDIO_ANALYSIS_MAX_ZCR` | `0.35  # above = broadband noise (with...` | yes |
| `AUDIO_ANALYSIS_MAX_SILENT_FRACTION` | `0.85` | yes |
| `AUDIO_ANALYSIS_WORDS_PER_SEC` | `2.7` | yes |
| `AUDIO_ANALYSIS_DURATION_OVERHEAD_SECS` | `1.0` | yes |
| `AUDIO_ANALYSIS_MAX_DURATION_RATIO` | `2.0  # over-long => repetition` | yes |
| `AUDIO_ANALYSIS_MIN_DURATION_RATIO` | `0.25  # too-short => truncation` | yes |
| `AUDIO_ANALYSIS_REGEN_CHARS_FACTOR` | `0.6` | yes |
| `AUDIO_ANALYSIS_REGEN_MIN_CHARS` | `150` | yes |
| `AUDIO_ANALYSIS_REGEN_PENALTY_STEP` | `0.15` | yes |

## Audio output, artwork, chapters

| Variable | Default | Runtime |
|---|---|---|
| `AUDIO_SILENCE_THRESHOLD` | `0.003` | yes |
| `AUDIO_SILENCE_BUFFER_MS` | `5` | yes |
| `AUDIO_MAX_INTERNAL_SILENCE_MS` | `1000` | yes |
| `AUDIO_INTERNAL_SILENCE_KEEP_MS` | `500` | yes |
| `LOUDNORM_TARGET_LUFS` | `-14` | yes |
| `LOUDNORM_TRUE_PEAK_DB` | `-3` | yes |
| `LOUDNORM_LRA` | `7` | yes |
| `MP3_BITRATE` | `128k` | yes |
| `MP3_SAMPLE_RATE` | `24000` | yes |
| `MP3_CHANNELS` | `2` | yes |
| `ARTWORK_SIZE_PX` | `3000` | yes |
| `EMBED_ARTWORK_SIZE_PX` | `1400` | yes |
| `ARTWORK_JPG_QUALITY` | `85` | yes |
| `ARTWORK_FETCH_TIMEOUT_SECONDS` | `15` | yes |
| `ARTWORK_MIN_SOURCE_PX` | `600` | yes |
| `ARTWORK_MAX_DOWNLOAD_BYTES` | `25 * 1024 * 1024` | yes |
| `CHAPTERS_ENABLED` | `True` | yes |
| `CHAPTERS_MIN_DURATION_SECS` | `600` | yes |

## Jobs, webhooks, operations

| Variable | Default | Runtime |
|---|---|---|
| `JOB_STALL_SECONDS` | `1800` | yes |
| `JOB_TIMEOUT_SECONDS` | `3600` | yes |
| `JOB_TIMEOUT_PER_CHUNK_SECONDS` | `30.0` | yes |
| `JOB_TIMEOUT_CEILING_MULTIPLIER` | `3.0` | yes |
| `QUEUE_POLL_INTERVAL_SECONDS` | `2.0` | env-only |
| `WEB_WORKERS` | `2` | env-only |
| `WEBHOOK_URL` | `` | yes |
| `WEBHOOK_TIMEOUT_SECONDS` | `10.0` | yes |
| `LOG_LEVEL` | `INFO` | yes |
| `LOG_FORMAT` | `json` | env-only |
| `DATA_DIR` | `Path("/data")` | env-only |
| `MIGRATION_BACKUP_RETENTION_DAYS` | `30` | env-only |

## Security (env-only on purpose)

Anything guarding the login path stays out of the runtime allowlist: reaching the Settings UI must not confer the ability to switch off the defences protecting it.

| Variable | Default | Runtime |
|---|---|---|
| `SESSION_SECRET_KEY` | `None` | env-only |
| `SESSION_COOKIE_SECURE` | `True` | env-only |
| `SESSION_COOKIE_MAX_AGE_SECONDS` | `86400 * 14` | env-only |
| `CORS_ORIGINS` | `` | env-only |
| `LOGIN_RATE_LIMIT` | `10/minute` | env-only |
| `LOCKOUT_MAX_FAILED_ATTEMPTS` | `5` | env-only |
| `LOCKOUT_WINDOW_SECONDS` | `15 * 60` | env-only |
| `TRUST_PROXY_HEADERS` | `False` | env-only |
| `TRUSTED_PROXY_HOPS` | `1` | env-only |

## The TTS wrapper's own environment

Set on the `tts-wrapper` service, read at its startup:

| Variable | Default | What it does |
|---|---|---|
| `TTS_DEVICE` | `cuda` | `cuda` or `cpu` |
| `TTS_LANGUAGE` | `en` | Default narration language |
| `TTS_REFERENCE_PATH` | `/app/reference/voice.wav` | Anchor for the voice slots directory |
| `TTS_SAMPLE_RATE` | `24000` | Provisional; the model's own rate replaces it at load |
| `TTS_REQUEST_TIMEOUT_SECONDS` | `120` | Per-request inference budget |
| `WHISPER_ENABLED` | `false` | Loads the faster-whisper model at startup |
| `WHISPER_MODEL` | `base` | faster-whisper model size |
| `TTS_MEMORY_SOFT_LIMIT_MB` | `8000` | RSS above this runs cleanup after a chunk; 0 disables |
| `TTS_MEMORY_HARD_LIMIT_MB` | `14000` | Still above this after cleanup restarts the wrapper between chunks; 0 disables |
| `MALLOC_ARENA_MAX` | `2` (compose) | Caps glibc arenas, bounding allocator growth |

The memory limits are the [memory ladder](how-it-works.md#the-wrappers-memory-ladder).

## The render sidecar's environment

`RENDER_URL` on the app points at it; the sidecar itself reads `LOG_FORMAT`/`LOG_LEVEL` and its own timeouts. It is optional by design: when unset or down, the app falls back to the ordinary extraction cascade.

[< Docs index](README.md)
