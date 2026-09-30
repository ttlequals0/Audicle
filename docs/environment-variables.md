# Environment variables

Only `BUILD_VERSION` is required in the stock Compose `.env` file. Set it to an existing release tag. Configure the app through Settings or the authenticated API after startup.

Saved settings override legacy environment values, which override code defaults. Existing deployments can keep their environment files. Sending `null` to `PUT /api/v1/settings` removes an override; an empty string is an explicit empty value for text settings. Secrets are masked in API responses.

Storage paths, worker count, the session signing key, hardware assignment, and container networking are deployment settings. Changing them requires recreating the relevant container. `.env.example` lists these optional overrides. Application controls below are available at runtime unless marked env-only; job settings apply to the next job.

The defaults below come from the backend configuration model. A deployed server's `GET /api/v1/settings` response reports its environment defaults and saved overrides.

## Identity and feed

The public base URL determines feed and media links. Changing it updates generated links without rotating podcast identity; subscribers may need the new subscription URL if the host changes.

| Variable | Default | Runtime |
|---|---|---|
| `BASE_URL` | `http://localhost:8000` | yes |
| `UI_BASE_URL` | `` | yes |
| `FEED_TITLE` | `Audicle` | yes |
| `FEED_DESCRIPTION` | `Articles read aloud by Audicle.` | yes |
| `FEED_AUTHOR` | `` | yes |
| `FEED_EMAIL` | `` | yes |
| `FEED_LANGUAGE` | `en-us` | yes |
| `FEED_CATEGORY` | `News` | yes |
| `FEED_EXPLICIT` | `False` | yes |
| `FEED_ARTWORK_URL` | `` | yes |
| `DEFAULT_ARTWORK_URL` | `https://raw.githubusercontent.com/ttlequals0/Audicle/main/branding/podcast-artwork-3000.jpg` | yes |
| `FEED_AUTH_ENABLED` | `False` | yes |
| `FEED_AUTH_KEY` | `` | yes |
| `RSS_CACHE_MAX_AGE_SECONDS` | `300` | yes |
| `RETENTION_DAYS` | `90` | yes |
| `RETENTION_SWEEP_HOUR_UTC` | `7` | yes |

## LLM provider

See [LLM providers](llm-providers.md). Keys are masked in API responses; protect the database and its backups.

| Variable | Default | Runtime |
|---|---|---|
| `LLM_PROVIDER` | `openai-compatible` | yes |
| `LLM_MODEL` | `` | yes |
| `OPENAI_BASE_URL` | `` | yes |
| `OPENAI_API_KEY` | `` | yes |
| `ANTHROPIC_API_KEY` | `` | yes |
| `OPENROUTER_API_KEY` | `` | yes |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434/v1` | yes |
| `LLM_TEMPERATURE` | `0.7` | yes |
| `LLM_REASONING_EFFORT` | `none` | yes |
| `LLM_MAX_TOKENS` | `16000` | yes |
| `LLM_CLEANUP_WINDOW_CHARS` | `12000` | yes |
| `LLM_TIMEOUT_SECONDS` | `300` | yes |
| `LLM_RETRY_COUNT` | `3` | yes |
| `LLM_PRONUNCIATION_CONCURRENCY` | `4` | yes |
| `PRONUNCIATION_SCOPE` | `chunk` | yes |

## Extraction and paywalls

The cascade and its budgets. See the paywalls page.

| Variable | Default | Runtime |
|---|---|---|
| `EXTRACTION_ENGINE` | `direct` | yes |
| `EXTRACTION_DIRECT_TIMEOUT_SECONDS` | `30` | yes |
| `EXTRACTION_DIRECT_USER_AGENT` | `` | yes |
| `EXTRACTION_ARC_ENABLED` | `True` | yes |
| `EXTRACTION_FALLBACKS_ENABLED` | `True` | yes |
| `MIN_EXTRACTION_CHARS` | `150` | yes |
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
| `FLARESOLVERR_MAX_TIMEOUT_MS` | `60000` | yes |
| `READER_PROXY_TEMPLATE` | `https://r.jina.ai/{url}` | yes |
| `READER_API_KEY` | `` | yes |
| `READER_AUTO_ENABLED` | `True` | yes |
| `RENDER_URL` | `` | yes |
| `RENDER_TIMEOUT_SECONDS` | `150.0` | yes |
| `ARCHIVE_FALLBACK_ENABLED` | `True` | yes |
| `WAYBACK_TIMEOUT_SECONDS` | `30` | yes |

## Cleanup and uploads

| Variable | Default | Runtime |
|---|---|---|
| `MIN_CLEANUP_CHARS` | `200` | yes |
| `CLEANUP_MIN_RETENTION_RATIO` | `0.5` | yes |
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
| `TTS_CHUNK_CACHE_ENABLED` | `True` | yes |
| `TTS_CACHE_RETENTION_DAYS` | `7` | yes |
| `TTS_ADAPTIVE_MAX_CHARS_ENABLED` | `True` | yes |
| `TTS_IDLE_RESTART_ENABLED` | `True` | yes |
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
| `WHISPER_API_STRICT` | `False` | yes |
| `WHISPER_DIVERGENCE_THRESHOLD` | `0.35` | yes |
| `WHISPER_MAX_DIVERGENT_RUN` | `8` | yes |
| `WHISPER_VERIFY_MIN_WORDS` | `8` | yes |
| `WHISPER_SHORT_CHUNK_DIVERGENCE` | `0.8` | yes |
| `AUDIO_ANALYSIS_ENABLED` | `True` | yes |
| `AUDIO_ANALYSIS_MAX_REGEN` | `2` | yes |
| `AUDIO_ANALYSIS_FRAME_MS` | `25` | yes |
| `AUDIO_ANALYSIS_HOP_MS` | `10` | yes |
| `AUDIO_ANALYSIS_WINDOW_SECS` | `3.0` | yes |
| `AUDIO_ANALYSIS_MAX_F0_SEMITONES` | `2.0` | yes |
| `AUDIO_ANALYSIS_F0_WARMUP_CHUNKS` | `3` | yes |
| `AUDIO_ANALYSIS_MIN_RMS_CV` | `0.35` | yes |
| `AUDIO_ANALYSIS_MIN_CREST` | `3.0` | yes |
| `AUDIO_ANALYSIS_MAX_ZCR` | `0.35` | yes |
| `AUDIO_ANALYSIS_MAX_SILENT_FRACTION` | `0.85` | yes |
| `AUDIO_ANALYSIS_WORDS_PER_SEC` | `2.7` | yes |
| `AUDIO_ANALYSIS_DURATION_OVERHEAD_SECS` | `1.0` | yes |
| `AUDIO_ANALYSIS_MAX_DURATION_RATIO` | `2.0` | yes |
| `AUDIO_ANALYSIS_MIN_DURATION_RATIO` | `0.25` | yes |
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
| `ARTWORK_MAX_DOWNLOAD_BYTES` | `26214400` | yes |
| `CHAPTERS_ENABLED` | `True` | yes |
| `CHAPTERS_MIN_DURATION_SECS` | `600` | yes |

## Jobs, webhooks, operations

| Variable | Default | Runtime |
|---|---|---|
| `JOB_STALL_SECONDS` | `1800` | yes |
| `JOB_TIMEOUT_SECONDS` | `3600` | yes |
| `JOB_TIMEOUT_PER_CHUNK_SECONDS` | `30.0` | yes |
| `JOB_TIMEOUT_CEILING_MULTIPLIER` | `3.0` | yes |
| `QUEUE_POLL_INTERVAL_SECONDS` | `2.0` | yes |
| `WEB_WORKERS` | `2` | env-only |
| `WEBHOOK_URL` | `` | yes |
| `WEBHOOK_TIMEOUT_SECONDS` | `10.0` | yes |
| `LOG_LEVEL` | `INFO` | yes |
| `LOG_FORMAT` | `json` | yes |
| `DATA_DIR` | `/data` | env-only |
| `MIGRATION_BACKUP_RETENTION_DAYS` | `30` | yes |

## Security

Login protection and cookie policy are runtime settings. Only the signing key stays outside the API; when omitted, Audicle generates and persists it. Keep proxy trust disabled unless a trusted reverse proxy supplies the client address.

| Variable | Default | Runtime |
|---|---|---|
| `SESSION_SECRET_KEY` | `` | env-only |
| `SESSION_COOKIE_SECURE` | `True` | yes |
| `SESSION_COOKIE_MAX_AGE_SECONDS` | `1209600` | yes |
| `LOGIN_RATE_LIMIT` | `10/minute` | yes |
| `LOCKOUT_MAX_FAILED_ATTEMPTS` | `5` | yes |
| `LOCKOUT_WINDOW_SECONDS` | `900` | yes |
| `TRUST_PROXY_HEADERS` | `False` | yes |
| `TRUSTED_PROXY_HOPS` | `1` | yes |

## TTS wrapper settings

Use the TTS wrapper controls in Settings or `PUT /api/v1/settings/sidecars`. Memory limits, idle unloading, inference timeout, Whisper options, and logging apply at runtime. Saved values survive wrapper restarts; the app reapplies them when the sidecar is reachable. The environment remains the reset default. Device allocation, reference paths, and the allocator remain deployment settings.

| Variable | Default | What it does |
|---|---|---|
| `TTS_DEVICE` | `cuda` | `cuda` or `cpu` |
| `TTS_IDLE_UNLOAD_SECONDS` | `300` | Unload idle GPU models after this many seconds; `0` disables unloading |
| `TTS_LANGUAGE` | `en` | Default narration language |
| `TTS_REFERENCE_PATH` | `/app/reference/voice.wav` | Anchor for the voice slots directory |
| `TTS_SAMPLE_RATE` | `24000` | Provisional; the model's own rate replaces it at load |
| `TTS_REQUEST_TIMEOUT_SECONDS` | `120` | Per-request inference budget |
| `WHISPER_ENABLED` | `false` | Enable lazy loading of faster-whisper for verification |
| `WHISPER_DEVICE` | follows `TTS_DEVICE` | Verification device: CPU or CUDA |
| `WHISPER_COMPUTE_TYPE` | `float16` on CUDA, `int8` on CPU | Verification model compute type |
| `WHISPER_MODEL` | `base` | faster-whisper model size |
| `TTS_MEMORY_SOFT_LIMIT_MB` | `8000` | RSS above this runs cleanup after a chunk; 0 disables |
| `TTS_MEMORY_HARD_LIMIT_MB` | `14000` | Still above this after cleanup restarts the wrapper between chunks; 0 disables |
| `MALLOC_ARENA_MAX` | `2` (compose) | Caps glibc arenas, bounding allocator growth |

The memory limits are the [memory ladder](how-it-works.md#the-wrappers-memory-ladder).

## Renderer settings

`RENDER_URL` points at the optional sidecar. Clear it in Settings to disable rendering. The bundled Compose stack supplies its internal URL by default. Saved rendering controls apply to the next extraction request without changing other requests. The total budget is also bounded by the app's HTTP timeout.

| Runtime setting | Default | Unit |
|---|---|---|
| `RENDER_NAV_TIMEOUT_MS` | `45000` | milliseconds |
| `RENDER_CLICK_TIMEOUT_MS` | `5000` | milliseconds |
| `RENDER_GROW_WAIT_MS` | `1500` | milliseconds |
| `RENDER_ATTEMPTS` | `3` | attempts |
| `RENDER_SETTLE_POLL_MS` | `300` | milliseconds |
| `RENDER_SETTLE_QUIET_POLLS` | `2` | unchanged polls |
| `RENDER_SETTLE_MAX_MS` | `8000` | milliseconds |
| `RENDER_BUDGET_SECONDS` | `120` | seconds |

Both sidecars also expose runtime `LOG_LEVEL` and `LOG_FORMAT` controls. Their defaults are `INFO` and `json`, unless their container environment sets another value. Settings reports pending changes when a sidecar cannot apply them yet.

The renderer network and firewall settings remain deployment configuration:

| Variable | Default | What it does |
|---|---|---|
| `RENDER_PROXY_IP` | `172.30.0.3` | Fixed IPv4 address of the renderer egress proxy on its internal network |
| `RENDER_PROXY_PORT` | `3128` | Proxy listener port |
| `RENDER_NETWORK_SUBNET` | `172.30.0.0/24` | Dedicated internal renderer-control subnet |
| `RENDER_CLIENT_IP` | `172.30.0.2` | Fixed renderer IPv4 address allowed by the proxy |

Choose an unused private subnet if the default overlaps another Docker network. Keep all four values consistent; startup fails closed when an address or port is invalid.

[< Docs index](README.md)
