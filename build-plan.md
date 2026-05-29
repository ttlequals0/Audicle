# Audicle: Build Plan

> **Your reading list, as a podcast you own.**
>
> Audicle (audio + article) -- a self-hosted service that turns articles into a Podcasting 2.0 compliant podcast feed.
>
> Repo: https://github.com/ttlequals0/Audicle

## Brand

**Name:** Audicle

**Mark:** Custom letter "A" with a five-bar audio waveform inside the counter. The waveform reads as a symmetric audio level meter (12 / 26 / 38 / 26 / 12 unit heights at icon scale).

**Palette:**

| Token | Hex |
|-------|-----|
| primary | `#1ce783` |
| background | `#040405` |
| paper | `#0a0a0c` |
| surface | `#15151a` |
| line | `#26262e` |
| text | `#f5f5f5` |
| text_dim | `#9a9aaa` |
| text_mute | `#6b6b78` |
| danger | `#ff5252` |

**Primary lockup:** Green mark on rich-black background. Used for podcast artwork and README header.

**Typography:**

- **Sans:** Satoshi (Fontshare). Used for display and UI body. Weights 400/500/700/900.
- **Mono:** JetBrains Mono. Used for technical accents: IDs, timestamps, status tags, section labels (`// SECTION_NAME` style), version strings.
- Inter is explicitly avoided.

**Assets:** see `logo-spec.md` for full construction details. SVG sources, rasterized PNGs, and design tokens (`tokens.json`, `tokens.css`) live at `branding/` (single source of truth across backend and frontend).

## Overview

A self-hosted service that ingests article URLs, cleans them up with an LLM, narrates them with a voice-cloned TTS, and serves the result as a Podcasting 2.0 compliant RSS feed.

This is not a NotebookLM-style two-speaker dialogue podcast. The LLM removes site cruft and normalizes text for speech; the TTS reads the article faithfully in a single narrator voice.

**Default deployment shape:** single user, LAN-administered, with the public feed (RSS, MP3, VTT, JPG) exposed by a tunnel or reverse proxy of the operator's choosing. The reference setup uses Cloudflare Tunnel and a Pocket Casts UA lock at the WAF layer, but any HTTPS exposure that restricts paths to `/rss/*` and `/media/*` works (e.g. Tailscale Funnel, ngrok, nginx-proxy-manager, plain Nginx with TLS).

Admin endpoints (`/api/v1/*`) are LAN-only by deployment, not by code. Auth is intentionally absent inside the LAN perimeter.

## Architecture

### Stack

Python target: the backend requires `>=3.13` and ships on a `python:3.14-slim` runtime image. The TTS wrapper is pinned to Python 3.11 because the `coqui-tts` (idiap fork) wheel currently caps at `<3.13`. See README for the Coqui TTS install caveat.

| Component | Role |
|-----------|------|
| FastAPI | HTTP server, API endpoints, RSS generation, static media serving |
| Pydantic + pydantic-settings | Config loading from env vars with type validation, runtime models |
| SQLite + aiosqlite | Persistent job state, episode catalog, transcripts |
| Background asyncio task | Job queue (SQLite-as-queue, single in-flight job) |
| httpx | Async HTTP client for Firecrawl, LLM endpoints, TTS wrapper |
| tenacity | Retry with exponential backoff (Firecrawl, transient network errors) |
| stdlib logging + custom JSONFormatter | Structured logging shipped to Loki. Lifted from MinusPod. |
| Firecrawl (self-hosted) | URL to markdown extraction |
| LLM (multi-provider) | Article cleanup, normalization for speech |
| XTTS-v2 wrapper (separate container) | Voice-cloned TTS synthesis |
| ffmpeg | Audio trim, concat, normalization, MP3 encode |
| Pillow | Artwork resize and format conversion |
| mutagen | Audio duration metadata |
| feedgen (with manual PC2 extension) | RSS XML generation (see note below) |
| Cloudflare Tunnel (optional) | Reference public-exposure option; any tunnel or reverse proxy works |

### Service Topology

Core containers in the docker-compose stack:

1. **app** -- runs two processes:
   - `uvicorn` serving HTTP (UI, API, RSS, media). Worker count configurable via `WEB_WORKERS` (default 2) for UI responsiveness.
   - `python -m app.worker` running the SQLite-backed queue and pipeline. One process, one in-flight job.
   - Both started by `entrypoint.sh`. Both read from the same SQLite DB (WAL mode for concurrent access). No in-memory caching; status reads always hit the DB so workers stay in sync.
2. **tts-wrapper** -- XTTS-v2 HTTP server, GPU-pinned. Single uvicorn worker. Async lock serializes GPU access; `/health` stays responsive.

Optional public-exposure container (operator's choice):

3. **cloudflared** (Cloudflare Tunnel daemon) -- reference option. Swap for Tailscale Funnel, ngrok, or a reverse-proxy container as needed. Or run the app behind your existing edge.

State persistence: SQLite database file plus all generated media (MP3, JPG, intermediate WAVs) live in the `./data` bind-mounted volume on the host. The volume is shared between `app` and `tts-wrapper` so the wrapper can write WAVs the app then reads.

External dependencies (not in this stack):
- **Ollama or other LLM endpoint.** Must be HTTP-reachable from inside the app container. Common patterns: `host.docker.internal:11434` for host-installed Ollama on Docker Desktop (Linux needs `extra_hosts: ["host.docker.internal:host-gateway"]` in compose); host LAN IP; or a separate Docker network if Ollama is containerized.
- **Firecrawl instance.** Must be HTTP-reachable from the app container. Easiest path: put Audicle's app container on the same Docker network as the Firecrawl stack and reference by container name.

Both endpoints are health-checked in the worker process at startup before the queue loop begins (not in the web process, which serves the UI/RSS regardless). Failures are logged and the worker exits non-zero; entrypoint supervision then restarts the container rather than running blind and failing every job at the relevant stage. See "Startup Reachability Checks" below.

### Project Layout

```
audicle/
|-- docker-compose.yml
|-- docker-compose.dev.yml         # bind mounts for live edits
|-- .env.example                   # template, checked in
|-- .env                           # operator-created, gitignored
|-- .gitignore
|-- .dockerignore
|-- pyproject.toml
|-- Dockerfile                     # backend (multi-stage: Node build for frontend, Python runtime)
|-- entrypoint.sh                  # starts uvicorn + worker, supervises both
|-- LICENSE                        # MIT
|-- README.md
|-- branding/                      # canonical brand assets, single source of truth
|   |-- README.md                  # palette, typography, usage
|   |-- tokens.json                # design tokens (color, type)
|   |-- tokens.css                 # CSS custom properties for frontend consumers
|   |-- mark.svg
|   |-- mark-mono.svg
|   |-- wordmark.svg
|   |-- wordmark-mono.svg
|   |-- podcast-artwork.svg
|   |-- podcast-artwork-3000.png
|   |-- podcast-artwork-1400.png
|   |-- favicon.svg
|   |-- favicon-32.png
|   `-- favicon-16.png
|-- backend/
|   |-- app/
|   |   |-- __init__.py
|   |   |-- main.py                # FastAPI app, lifespan
|   |   |-- worker.py             # queue process entry point (python -m app.worker)
|   |   |-- config.py              # Pydantic BaseSettings
|   |   |-- version.py             # __version__ single source of truth
|   |   |-- api/
|   |   |   |-- health.py            # /health/live, /health/ready, /health
|   |   |   |-- media.py             # /media/{id}.mp3 / .jpg / .vtt
|   |   |   |-- rss.py               # /rss/rss.xml
|   |   |   |-- errors.py            # shared error-envelope handlers
|   |   |   |-- deps.py              # require_admin + shared deps
|   |   |   `-- v1/
|   |   |       |-- router.py
|   |   |       |-- submit.py
|   |   |       |-- status.py
|   |   |       |-- jobs.py
|   |   |       |-- episodes.py        # list/delete
|   |   |       |-- prompt.py
|   |   |       |-- corrections.py
|   |   |       |-- settings.py        # runtime overrides
|   |   |       |-- reference.py       # preview/test/commit
|   |   |       |-- auth.py            # login/logout/status
|   |   |       `-- purge.py
|   |   |-- core/
|   |   |   |-- database.py          # schema, migrations, WAL, backups
|   |   |   |-- paths.py
|   |   |   `-- timestamps.py
|   |   |-- services/                 # dataclasses (Job, Episode, ...) live in
|   |   |   |                         # their service modules; no separate models/ dir
|   |   |   |-- extraction.py      # Firecrawl client
|   |   |   |-- llm.py             # multi-provider abstraction
|   |   |   |-- tts.py             # XTTS wrapper client
|   |   |   |-- audio.py           # trim, concat, normalize, encode (soundfile + ffmpeg)
|   |   |   |-- chunker.py         # hybrid paragraph/sentence/comma chunking
|   |   |   |-- transcript.py      # WebVTT generation
|   |   |   |-- pipeline.py        # stage orchestration
|   |   |   |-- jobs.py            # job-table helpers + queue claim (worker.py drives it)
|   |   |   |-- episodes.py        # episode-table upsert/list helpers
|   |   |   |-- retention.py       # daily sweep (episodes, jobs, orphan media)
|   |   |   |-- feed.py            # RSS generation
|   |   |   |-- artwork.py         # og:image fetch + crop/resize/JPG
|   |   |   |-- corrections.py     # pronunciation dict load/validate/apply
|   |   |   |-- prompt.py          # cleanup-prompt file load/save
|   |   |   |-- runtime_settings.py # default->env->DB overlay + allowlist
|   |   |   |-- settings_store.py  # settings table (podcast guid, etc.)
|   |   |   |-- reachability.py    # startup dependency probes
|   |   |   |-- atomic_write.py    # atomic file replace
|   |   |   |-- auth.py            # password verify, sessions, lockout
|   |   |   `-- csrf.py            # token issue/validate
|   |   |-- utils/
|   |   |   `-- logging.py
|   |   |-- prompts/
|   |   |   `-- script.txt         # bind-mounted, editable
|   |   |-- corrections/
|   |   |   `-- pronunciation.json # bind-mounted, editable
|   |   |-- reference/
|   |   |   |-- README.md          # specs + sources for voice.wav
|   |   |   `-- voice.wav          # operator-supplied, gitignored
|   |   `-- assets/                # build-time copy of needed branding files for FastAPI to serve
|   |-- tests/
|   |   `-- conftest.py
|   `-- scripts/
|-- frontend/                      # React 18 + Vite + TypeScript + Tailwind, PWA-capable
|   |-- package.json
|   |-- vite.config.ts
|   |-- tsconfig.json
|   |-- tailwind.config.js
|   |-- index.html
|   |-- public/
|   |   |-- manifest.json
|   |   `-- (favicons, copied from /branding at build time)
|   `-- src/
|       |-- main.tsx
|       |-- App.tsx
|       |-- routes/
|       |   |-- Home.tsx           # URL submission form
|       |   |-- Feed.tsx           # episode cards list with reprocess action
|       |   `-- Settings.tsx       # tunables, prompt editor, reference audio
|       |-- components/
|       `-- api/                   # React Query hooks for backend endpoints
|-- tts-wrapper/
|   |-- Dockerfile
|   |-- main.py                    # FastAPI wrapping XTTS-v2
|   `-- requirements.txt
`-- data/                          # host-mounted volume
    |-- podcast.db
    `-- media/
        |-- {episode_id}.mp3
        |-- {episode_id}.jpg
        `-- (transcripts stored in DB, served on-demand as .vtt)
```

## Pipeline

Before any pipeline processing begins, startup reachability checks confirm Firecrawl, the LLM endpoint, and the TTS wrapper are healthy. See "Startup Reachability Checks" under Failure Handling for details. Failures cause the app to exit non-zero rather than running blind.

End-to-end per submitted URL:

1. **Submit:** `POST /api/v1/submit` writes job row to SQLite with status `queued`, returns job_id and computed episode_id (MD5 hash of URL truncated to 12 hex chars). Returns 409 if episode_id already exists, unless `reprocess=true` flag is set.
2. **Queue:** background asyncio task polls SQLite for `queued` jobs. Single in-flight job, no concurrent processing.
3. **Extract (stage `extract`):** Firecrawl HTTP call with 3 retries (exponential backoff), returns markdown. Validate against minimum length threshold; fail if too short.
4. **Cleanup (stage `cleanup`):** send markdown to LLM with cleanup prompt. LLM removes site cruft, transforms headings to transitions, summarizes code blocks and tables, normalizes URLs and symbols. Output is plain text.
5. **Corrections (stage `corrections`):** apply pronunciation dictionary substitutions to cleaned text.
6. **Chunk (stage `chunk`):** split text on paragraph boundaries first, sentence boundaries if paragraph exceeds size limit, comma or semicolon boundaries as fallback for long sentences. Target 180 words per chunk, hard max 220.
7. **Synthesize (stage `tts`):** for each chunk, POST to TTS wrapper at `/generate`. Wrapper returns WAV path on shared volume. Track per-chunk duration.
8. **Audio post-process (stage `audio`):** trim silence from each chunk, concat WAVs with configurable silence padding (default 250ms), normalize with ffmpeg filter chain (lifted from ebook2audiobook: loudnorm -14 LUFS, EQ, denoise, compress), encode to MP3.
9. **Artwork (stage `artwork`):** download og:image from article metadata, center-crop to square, resize to 3000x3000 JPG, quality 85, strip EXIF. Fall back to feed-level artwork if missing or broken.
10. **Transcript (stage `transcript`):** generate VTT from chunk text + tracked durations. Store in `transcript_vtt` column.
11. **Finalize (stage `finalize`):** insert/update episode row with MP3 path, JPG path, duration, transcript. Set job status to `done`.

If any stage fails: write error message to `jobs.error`, write failing stage to `jobs.stage`, set status to `failed`. Job is not retried automatically. User can resubmit with `reprocess=true` to retry.

### Queue Polling

The background asyncio task polls the `jobs` table for `status='queued'` rows every `QUEUE_POLL_INTERVAL_SECONDS` (default 2). One in-flight job at a time. After picking up a job, the task processes through all stages sequentially before polling again.

For this scale (single-user feed, minutes-per-episode processing time), 2-second polling latency is negligible. Tighten the interval only if submission-to-start latency starts to matter.

### Stage Logging

Each stage emits structured log records at start and end. Default detail at `LOG_LEVEL=INFO`:

- **Stage start:** `stage=<name>`, `event=stage_start`, `job_id`, `episode_id`
- **Stage end:** `stage=<name>`, `event=stage_end`, `duration_ms`, `job_id`, `episode_id`, plus stage-specific counters (e.g., `chunk_count` for the chunk stage, `retries_used` for extract)
- **Stage failure:** `stage=<name>`, `event=stage_failed`, `error`, exception with traceback

At `LOG_LEVEL=DEBUG`, every meaningful operation logs (each chunk synthesis, each retry attempt, each LLM call with token counts). Useful for development and one-off troubleshooting; too noisy for steady-state.

### Job Timeout

The entire per-job pipeline runs under `asyncio.wait_for(process_job(...), timeout=JOB_TIMEOUT_SECONDS)`. Default ceiling is 30 minutes.

For the timeout handler to know which stage was executing when the deadline fired, each stage writes its name to `jobs.stage` at the start of execution before doing any work. On timeout, the job is marked `failed`, `jobs.stage` reflects the last stage that began, and `jobs.error` captures `"job exceeded JOB_TIMEOUT_SECONDS during stage <name>"`.

### Per-Stage Retries

Some stages have transient failure modes worth retrying. Others either always work or always fail in the same way; retry doesn't help.

| Stage | Retry policy |
|-------|--------------|
| extract | 3 attempts (`FIRECRAWL_RETRY_COUNT`), tenacity at HTTP layer, exponential backoff |
| cleanup | 3 attempts (`LLM_RETRY_COUNT`), tenacity at LLM call, exponential backoff |
| tts (per chunk) | 3 attempts (`TTS_RETRY_COUNT`), tenacity at wrapper HTTP call, exponential backoff |
| artwork | 1 attempt; on failure, falls back to feed-level artwork (graceful degrade, episode still publishes) |
| corrections, chunk, audio, transcript, finalize | No retry. Pure functions or local DB writes; if they fail, retry won't help. |

Retryable errors: network timeouts, 5xx responses, connection errors. Non-retryable: 4xx responses, validation failures, our own assertion errors.

### Crash Recovery

On startup, the queue worker executes:

```sql
UPDATE jobs SET status='queued', error='reset on restart' WHERE status='processing';
```

Jobs that died mid-processing pick back up. No external job state to recover.

## Data Model

SQLite at `$DATA_DIR/podcast.db`. Connections open with `PRAGMA journal_mode=WAL` at startup so the HTTP workers and the queue worker can read/write concurrently without locking each other out.

Timestamps use `TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))` for ISO 8601 UTC strings. `updated_at` is application-managed: every UPDATE statement explicitly sets `updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')`. No triggers (lifted from MinusPod's pattern).

```sql
CREATE TABLE jobs (
    id           TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    episode_id   TEXT NOT NULL,
    status       TEXT NOT NULL,   -- queued | processing | done | failed
    stage        TEXT,            -- extract | cleanup | corrections | chunk | tts | audio | artwork | transcript | finalize | done
    error        TEXT,
    created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE episodes (
    id              TEXT PRIMARY KEY,   -- MD5(url)[:12], also the episode_id from jobs
    job_id          TEXT REFERENCES jobs(id),
    title           TEXT,
    author          TEXT,
    original_url    TEXT NOT NULL UNIQUE,
    audio_path      TEXT,               -- /data/media/{id}.mp3
    artwork_path    TEXT,               -- /data/media/{id}.jpg
    transcript_vtt  TEXT,               -- VTT content stored in DB
    duration_secs   INTEGER,
    pub_date        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_episodes_pub_date ON episodes(pub_date DESC);
```

**Episode timestamp semantics:**

- `created_at` is set once on insert and never changes, even on reprocess. Represents "when this article was first added to the feed."
- `pub_date` is set on insert and updated on each reprocess. Drives RSS `<pubDate>` and feed ordering. A reprocessed episode appears as new in podcast clients.
- `updated_at` bumps on every row mutation (status change, field update). Useful for debugging and stale-row detection.

### Schema Migrations

Contract: migrations run automatically at startup with no data loss. Operators never run migration commands by hand.

Mechanism (lifted from MinusPod):

- **Idempotent migration methods.** Each migration is a Python method that checks current schema state and applies the change only if needed. Safe to run multiple times.
- **File-locked.** A `.migration.lock` file in `DATA_DIR` serializes concurrent startups (multi-container or rapid restarts) so two processes can't migrate at once.
- **Backup before migration.** Before applying any pending migration, the app copies `podcast.db` to `podcast.db.backup-{timestamp}`. Backups older than `MIGRATION_BACKUP_RETENTION_DAYS` (default 30) are pruned by the daily retention sweep.
- **Destructive operations are wrapped in transactions.** Renames go through CREATE-new + INSERT-from-old + DROP-old, all in one transaction. If any step fails, the schema is unchanged.
- **Column drops are avoided.** Deprecated columns stay in the schema, marked unused in code, and removed only when a major version warrants a clean break.

This pattern scales fine for the project's expected migration count (a handful per year). It does not need Alembic, version tables, or migration files.

## API

### Public Routes (intended for external exposure)

```
GET  /rss/rss.xml                       # podcast RSS feed
GET  /media/{episode_id}.mp3            # episode audio
GET  /media/{episode_id}.jpg            # episode artwork
GET  /media/{episode_id}.vtt            # episode transcript (DB-backed)
```

### Admin Routes (LAN-only, restricted at the edge of operator's choice)

```
# Jobs and Episodes
POST   /api/v1/submit                   # submit article URL
GET    /api/v1/status/{job_id}          # job status
GET    /api/v1/jobs?status=failed       # job history (filterable)
GET    /api/v1/episodes                 # episode list (for Feed tab)
DELETE /api/v1/episodes/{episode_id}    # delete an episode

# Prompt and Corrections
GET    /api/v1/prompt                   # read current cleanup prompt
PUT    /api/v1/prompt                   # update cleanup prompt
GET    /api/v1/corrections              # read pronunciation dictionary
PUT    /api/v1/corrections              # update pronunciation dictionary

# Runtime Settings (UI-editable subset of env vars)
GET    /api/v1/settings                 # read current runtime overrides
PUT    /api/v1/settings                 # update a runtime setting

# Reference Audio
GET    /api/v1/reference/preview        # download the currently-installed voice.wav
POST   /api/v1/reference/test           # upload a candidate WAV + sample text, audition without committing
POST   /api/v1/reference/commit         # upload a candidate WAV, swap it into live voice.wav

# Retention
POST   /api/v1/purge                    # delete all episodes (requires confirm)
POST   /api/v1/purge?older_than_days=N  # partial purge

# Auth (when enabled)
GET    /api/v1/auth/status              # auth_enabled, logged_in, username, csrf_token. Always accessible.
POST   /api/v1/auth/login               # body: {username, password}. Rate-limited; returns csrf_token.
POST   /api/v1/auth/logout              # clears session.

# System
GET    /health                          # health check
```

### API Conventions

Per-endpoint contracts are auto-generated from FastAPI's type annotations. Swagger UI is available at `/api/v1/docs` when the app runs. A committed `openapi.yaml` at repo root is regenerated by `backend/scripts/dump_openapi.py` and reviewed via git diff on schema changes.

This section documents the conventions that apply across all endpoints.

#### Error Envelope

All error responses (4xx, 5xx) use a consistent shape, lifted from MinusPod:

```json
{
  "error": "human-readable message",
  "status": 400
}
```

For 4xx errors, an optional `details` object may include validation context (field name, expected format, etc.). For 5xx errors, `details` is logged server-side but never returned to the client to prevent internal leakage.

#### Status Codes

| Code | Meaning | When |
|------|---------|------|
| 200 | OK | Successful read or update |
| 201 | Created | Successful resource creation (e.g., `POST /api/v1/submit`) |
| 204 | No Content | Successful delete |
| 400 | Bad Request | Malformed input, validation failure |
| 401 | Unauthorized | No session when auth required, or login failed |
| 403 | Forbidden | CSRF token missing or invalid |
| 404 | Not Found | Resource doesn't exist |
| 409 | Conflict | Duplicate submit without `reprocess=true` |
| 413 | Payload Too Large | Upload exceeds size limit |
| 423 | Locked | IP locked out from repeated auth failures |
| 429 | Too Many Requests | Rate limit exceeded |
| 500 | Internal Server Error | Unhandled exception; `details` stripped |
| 503 | Service Unavailable | Startup checks failed or downstream dependency unreachable |

#### Pagination

List endpoints (`/api/v1/jobs`, `/api/v1/episodes`) accept:

- `?page=N` (1-based, default 1)
- `?per_page=M` (default 50, max 500)

Total count returned in the `X-Total-Count` response header.

```
GET /api/v1/episodes?page=3&per_page=20
X-Total-Count: 312
```

#### Timestamps

All timestamps in API responses are ISO 8601 UTC strings: `YYYY-MM-DDTHH:MM:SSZ`. DB values pass through unchanged; no timezone conversions happen in the API layer.

#### Content Types

- All JSON endpoints: `application/json` (request and response)
- File uploads: `multipart/form-data` (reference audio upload)
- Static media (`/media/*`, `/static/*`): appropriate per file (`audio/mpeg`, `image/jpeg`, `text/vtt`, etc.)

#### CSRF

When auth is enabled:

- State-changing requests (POST, PUT, DELETE) require an `X-CSRF-Token` header.
- The token is returned by `POST /api/v1/auth/login` and `GET /api/v1/auth/status` (double-submit cookie pattern).
- Token rotates on each login.
- Missing or invalid token returns 403.

Read-only requests (GET) do not require the header.

#### Upload Limits

- Reference audio (`POST /api/v1/reference/test` and `/commit`): max 5 MB (hardcoded `_MAX_REFERENCE_BYTES`), plus a 3-60 s WAV duration check. Returns 400 on oversize or an unreadable/out-of-range clip; the upload is rejected mid-stream once it crosses the cap.

### Example Contracts

The illustrative examples below show the conventions in practice. Full per-endpoint contracts are in the auto-generated OpenAPI spec.

**POST /api/v1/submit**

```json
// Request
{
  "url": "https://example.com/article",
  "reprocess": false
}

// Response 201 (new)
{
  "job_id": "uuid",
  "episode_id": "abc123def456",
  "status": "queued"
}

// Response 409 (duplicate, no reprocess flag)
{
  "error": "Episode already exists",
  "status": 409,
  "details": {
    "episode_id": "abc123def456",
    "url": "https://example.com/article"
  }
}
```

**GET /api/v1/status/{job_id}**

```json
{
  "job_id": "uuid",
  "episode_id": "abc123def456",
  "url": "https://example.com/article",
  "status": "processing",
  "stage": "tts",
  "error": null,
  "created_at": "2026-05-26T12:42:00Z",
  "updated_at": "2026-05-26T12:45:30Z"
}
```

## Authentication

Optional single-admin auth, lifted from MinusPod's pattern. Off by default
(`AUTH_ENABLED=false`); LAN-trust deployments leave it disabled. Operators who
expose the app more broadly enable it. The admin credential is supplied through
env vars (a username plus a pre-computed bcrypt hash), not set at runtime.

### Behavior

- **Off (`AUTH_ENABLED=false`):** all requests allowed; `require_admin` is a no-op. `GET /api/v1/auth/status` returns `{"auth_enabled": false, "logged_in": false, "username": "...", "csrf_token": "..."}`.
- **On (`AUTH_ENABLED=true`):** state-changing endpoints require an authenticated session plus a valid CSRF token. Read-only public routes (`/rss/*`, `/media/*`) are never gated.

### Endpoints

```
GET    /api/v1/auth/status         # auth_enabled, logged_in, username, csrf_token. Always accessible.
POST   /api/v1/auth/login          # body: {username, password}. Rate-limited; returns a csrf_token.
POST   /api/v1/auth/logout         # clears session.
```

There is no `set-password` or dedicated `/auth/csrf` endpoint: the password is an
env-supplied bcrypt hash (changed by editing `.env` + restart), and the CSRF
token is returned by `/auth/login` and `/auth/status`.

### Mechanics

- **Password storage:** the bcrypt hash is the `ADMIN_PASSWORD_HASH` env var (with `ADMIN_USERNAME`). Generate it with the helper command in `.env.example`. Never logged or returned by any endpoint. It is not stored in the DB.
- **Sessions:** Starlette `SessionMiddleware` with `SESSION_SECRET_KEY`. Required when auth is on (startup fails if absent); when auth is off an ephemeral per-process key is used (not persisted).
- **Cookie:** `SESSION_COOKIE_SECURE` defaults to **false** so localhost `http://` works; set true once HTTPS fronts the app. `SESSION_COOKIE_MAX_AGE_SECONDS` controls lifetime (default 14 days).
- **Rate limiting:** slowapi limits `/auth/login` to `10/minute` (the decorator value is currently hardcoded; `LOGIN_RATE_LIMIT` is advisory until wired).
- **Lockout:** repeated failures key on the (lowercased) **username**, not IP. `LOCKOUT_MAX_FAILED_ATTEMPTS` (default 5) opens a `LOCKOUT_WINDOW_SECONDS` window (default 900s) during which `/auth/login` returns 423. Table `auth_lockout`. There is no LAN/private-IP exemption.
- **CSRF:** state-changing endpoints (`POST`, `PUT`, `DELETE`) require an `X-CSRF-Token` header matching a double-submit cookie token. The token is issued by `/auth/login` and `/auth/status` and rotates on each login. Lifted from MinusPod's `csrf.py`.

### New Schema

```sql
CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE auth_lockout (
    identifier      TEXT PRIMARY KEY,   -- lowercased admin username, not IP
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    lockout_until   TEXT,
    updated_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE runtime_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
```

`runtime_settings` (used by Settings UI) is separate from `settings` (used by auth) to keep concerns split.

### Env Vars

```
AUTH_ENABLED=false                 # master switch; off = all requests allowed
ADMIN_USERNAME=admin               # required when AUTH_ENABLED=true
ADMIN_PASSWORD_HASH=               # required when AUTH_ENABLED=true; bcrypt hash (see .env.example helper)
SESSION_SECRET_KEY=                # required when AUTH_ENABLED=true; ephemeral per-process when off
SESSION_COOKIE_SECURE=false        # set true once HTTPS fronts the app
SESSION_COOKIE_MAX_AGE_SECONDS=1209600   # 14 days
LOCKOUT_MAX_FAILED_ATTEMPTS=5      # failed attempts before lockout
LOCKOUT_WINDOW_SECONDS=900         # 15-minute lockout window
LOGIN_RATE_LIMIT=10/minute         # advisory; decorator value is currently hardcoded
```

## Web UI

Three-tab progressive web app served from `/` by FastAPI as static files. Built from `frontend/` (React 18 + Vite + TypeScript + Tailwind + React Query + React Router). PWA capability via `vite-plugin-pwa` (install-to-homescreen; no offline mode).

Mobile-first. Designed for one-handed phone use first, scales up to desktop.

### Design System

Aesthetic direction: **terminal-utility podcast**. Developer-tool feel, not consumer app. Heavy display weight, generous green-on-black contrast, monospace accents for technical detail (IDs, timestamps, stages, version strings, error codes).

**Typography:**

- **Display / UI body:** Satoshi (Fontshare, free). Weights: 400, 500, 700, 900.
- **Mono accents:** JetBrains Mono. Weights: 400, 500, 700.
- Inter is explicitly avoided.

**Colors** (from `branding/tokens.json`):

```
ink     #040405   page background
paper   #0a0a0c   card background
surface #15151a   input background
line    #26262e   borders
mute    #6b6b78   subtle text
dim     #9a9aaa   secondary text
fg      #f5f5f5   primary text
green   #1ce783   primary accent
danger  #ff5252   errors, destructive actions
```

**Visual details:**

- Subtle SVG noise texture overlay across the app for depth (4% opacity, green-tinted).
- 1px gradient accent line under the header (green, fading at the edges).
- Section labels styled as code comments (`// SECTION_NAME`).
- Status tags in uppercase JetBrains Mono with pulsing dots for active states (queued, processing).
- Buttons: primary green-on-ink, ghost outlined, danger red-outlined.
- Rounded corners: 8px on inputs and small elements, 12px on cards.
- Custom mini audio player for reference voice preview.

**Mobile-specific:**

- `viewport-fit=cover` for notched phones.
- `env(safe-area-inset-top/bottom)` on the header and main content.
- `theme-color` meta tag matches app background so iOS chrome blends.
- All tap targets minimum 40px height.
- Form fields stack vertically on narrow screens.
- Card action rows use `flex-wrap` so buttons don't overflow.
- Submit button on Home is full-width for thumb reach.

### Tabs

**1. Home.** Centered URL input form. Single field for article URL, full-width submit button. Shows the most recent submission's URL, status badge, and stage info inline below. Posts to `/api/v1/submit`.

**2. Feed.** Card-based list of all processed episodes. Header row includes:
- Title ("Feed") and episode count
- **Copy feed URL** button: copies `{BASE_URL}/rss/rss.xml` to clipboard, shows brief "copied" confirmation. Lets the operator add the feed to a podcast client without typing.

Each card shows:
- Episode artwork (64x64 square)
- Title (linked to original article URL, 2-line clamp)
- Status badge (queued, processing+stage, done, failed+stage)
- Author and source domain (if available)
- Created timestamp
- Error message (if failed)
- Action row: Reprocess, View transcript, Delete. Reprocess disabled when status is queued or processing.

Cards ordered newest first. Pull-to-refresh on mobile (matching MinusPod). Paginated (`page`/`per_page`) if episode count grows.

Each card shows the artwork thumbnail, a status badge, the title linked to the source article (2-line clamp), the author and source domain, the episode id and duration, and an action row: mp3, transcript (`/media/{id}.vtt`), Reprocess (`POST /api/v1/submit` with `reprocess=true`), and Delete. The list is published-episode-backed, so the badge reads `done`; in-flight/failed job states surface on the Home tab.

**3. Settings.** Editable settings grouped by section. Six field groups exposed in the UI plus the prompt editor, corrections table, reference voice widget, and a read-only system-info block. The groups map to the runtime-settings allowlist (see "UI-Editable Subset"):

1. **LLM:** `LLM_PROVIDER` (dropdown), `LLM_MODEL`, `OPENAI_BASE_URL`, `OPENAI_API_KEY` (masked), `ANTHROPIC_API_KEY` (masked), `LLM_TEMPERATURE`, `LLM_MAX_TOKENS`, `LLM_TIMEOUT_SECONDS`, `LLM_RETRY_COUNT`. The API keys render as password inputs; the backend masks them on read and ignores the mask sentinel on save so re-saving never clobbers the stored secret.
2. **Feed:** `FEED_TITLE`, `FEED_DESCRIPTION`, `FEED_AUTHOR`, `FEED_EMAIL`, `FEED_LANGUAGE`, `FEED_CATEGORY`, `FEED_EXPLICIT`, `FEED_ARTWORK_URL`.
3. **TTS:** `TTS_CHUNK_TARGET_WORDS`, `TTS_CHUNK_MAX_WORDS`, `TTS_CHUNK_SILENCE_MS`. Plus the reference audio widget.
4. **Cleanup:** `MIN_CLEANUP_CHARS`, `MAX_PROMPT_LENGTH_BYTES`. Plus the cleanup-prompt editor (`GET/PUT /api/v1/prompt`) and the pronunciation-corrections table (`GET/PUT /api/v1/corrections`).
5. **Retention:** `RETENTION_DAYS`. Plus a danger-styled "Purge all" button (with confirmation).
6. **RSS:** `RSS_CACHE_MAX_AGE_SECONDS`.
7. **System** (read-only): auth state (`auth_enabled`, `logged_in`) and the count of operator-tunable keys.

When auth is enabled the UI gates writes behind login + CSRF; there is no in-UI password change (the hash is env-supplied).

The XTTS generation params and infrastructure paths (`TTS_URL`, `DATA_DIR`, etc.) stay env-only and require a container restart to change. Runtime overrides are applied per job by the worker (`runtime_settings.overlay`), so LLM/feed/chunk edits take effect on the next submission.

### Settings Persistence

Editable settings write to the `runtime_settings` DB table. Config resolution order:

1. Pydantic field default
2. Env var (if set)
3. `runtime_settings` row (if exists)

Last value wins. Config is re-read per job, so most UI changes take effect on the next submission without restart. Keys that affect startup (none currently in the exposed groups, but the mechanism is in place) would be flagged with a "restart required" badge.

Allowlist of editable keys lives in code. Unknown keys posted by the UI are rejected.

### Reference Audio Widget

Three-step flow visible in the TTS settings section:

1. **Current voice** at top: small player with play button, progress bar, duration. Shows filename, length, sample rate.
2. **Drop zone** below: drag-and-drop or click to choose file. Shows specs (6-12s, 22050+ Hz, mono, clean).
3. **Preview card** appears after upload: filename + duration, "Play upload" button, "TTS test" button (runs sample through wrapper with the uploaded voice), and a primary "Commit" button that swaps the live voice.

Backend flow (no persistent temp-ID staging; each call carries the candidate WAV directly):

1. `GET /api/v1/reference/preview` streams back the currently-installed `voice.wav` so the UI can play the live voice.
2. `POST /api/v1/reference/test` accepts a candidate WAV upload plus a `sample_text` field. Under an `asyncio.Lock` it atomically stages the candidate into the live reference path, calls the wrapper `/reload` + `/generate`, returns the generated audio, then restores the prior clip (or removes the candidate if none existed) before releasing the lock. Nothing is left committed.
3. `POST /api/v1/reference/commit` accepts a candidate WAV, validates it, atomically replaces `voice.wav`, and triggers the wrapper to reload embeddings (`POST /reload`).

Because `/test` stages and restores atomically rather than writing to `/data/tmp`, there are no reference temp files to sweep.

### PWA

`manifest.json` in `frontend/public/`:

```json
{
  "name": "Audicle",
  "short_name": "Audicle",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#040405",
  "theme_color": "#040405",
  "icons": [
    {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
    {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}
  ]
}

`theme_color` matches the app background (`#040405`) so iOS chrome blends, per
the design-system note above. The manifest is generated by `vite-plugin-pwa`
from `frontend/vite.config.ts`; the icon PNGs live in `frontend/public/`.
```

Service worker registered for install-to-homescreen support. No offline caching of API responses.

### Build Pipeline

`frontend/` builds with `vite build`, outputs to `frontend/dist/`. The backend Dockerfile copies `frontend/dist/` into `/app/static/` and FastAPI serves it via `StaticFiles` mounted at `/`. The build is a separate stage in the Dockerfile (Node base image for build, Python base for runtime).

Fonts loaded via Fontshare CDN in the HTML head. Preconnect hint included for performance.

## LLM: Multi-Provider Abstraction

Lifted from MinusPod. Two backends behind one interface.

### Interface

```python
# services/llm.py

async def generate(
    system_prompt: str,
    user_message: str,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """Send a prompt to the configured LLM provider and return the response text.

    Provider selection is driven by LLM_PROVIDER. Per-call kwargs override
    config defaults for that call (used by cleanup pipeline; not used for
    the prior-chunk summary calls).

    Raises:
        LLMTimeoutError: request exceeded LLM_TIMEOUT_SECONDS
        LLMProviderError: 5xx from provider, retryable
        LLMRequestError: 4xx from provider, not retryable
    """
```

The pipeline's `cleanup` stage wraps this with tenacity for retries on retryable errors. The pipeline doesn't know which provider is selected.

Internal implementation: a thin module that dispatches to either `_call_openai_compatible()` or `_call_anthropic()` based on `LLM_PROVIDER`. Both use httpx for transport.

Compatible providers for `openai-compatible` mode: Ollama, vLLM, LM Studio, OpenRouter, Groq, Fireworks, llama.cpp server, any service exposing OpenAI's `/v1/chat/completions` endpoint shape.

### Configuration

Configurable via env:

```
LLM_PROVIDER=openai-compatible   # or "anthropic"
LLM_MODEL=                       # required, no default (example: qwen2.5:14b)
OPENAI_BASE_URL=http://host.docker.internal:11434/v1   # if LLM_PROVIDER=openai-compatible
OPENAI_API_KEY=not-needed                              # if LLM_PROVIDER=openai-compatible
ANTHROPIC_API_KEY=               # required if LLM_PROVIDER=anthropic
LLM_TEMPERATURE=0.7
LLM_MAX_TOKENS=4000
LLM_TIMEOUT_SECONDS=300
LLM_RETRY_COUNT=3
```

Validation at startup: required vars for selected provider must be present, fail fast otherwise.

### Cleanup Prompt

Lives at `backend/app/prompts/script.txt`, bind-mounted, editable via `PUT /api/v1/prompt`. Re-read on every job (no caching).

Both edit paths write to the same file: the bind mount lets operators edit `prompts/script.txt` on the host directly, and `PUT /api/v1/prompt` writes to the same path inside the container. File-system edits and API edits are interchangeable; the next job picks up whichever was most recent.

Behavior:

- **Remove:** navigation, menus, headers, footers, newsletter blocks, related-article sections, social share, cookie banners, comments, author bios, image captions, pull quotes, footnotes, article metadata (date, byline, read time).
- **Replace with summary:** code blocks (one-sentence description of what the code does, read the code to understand it), tables (description of what the table compares).
- **Transform:** headings into natural transition sentences in context.
- **Normalize:** URLs to "link to [domain]", inline code to natural language, symbols (%, &, $, @) to spoken words.
- **Leave alone:** numbers, acronyms, version numbers, block quotes from real people in the article, and all body prose not matched by the rules above.

Output: plain text, no markdown, no preamble. Paragraphs separated by blank lines (`\n\n`).

Prompt uses system message for rules, user message for article. Few-shot with 1-2 short examples.

## TTS: XTTS-v2 Wrapper

Separate container, FastAPI wrapping Coqui TTS package.

### Wrapper Contract

```
POST /generate
{
  "text": "...",            // a single chunk of cleaned text
  "episode_id": "...",      // for output filename and logging
  "chunk_index": 0          // 0-based chunk position, for filename and logging
}

200 {
  "wav_path": "/data/media/{episode_id}_chunk_{chunk_index}.wav",
  "duration_secs": 12.3,
  "sample_rate": 24000
}
```

The caller (main app) chunks the cleaned text and calls `/generate` once per chunk with the chunk's index. The wrapper writes the WAV to the shared volume and returns the path plus duration.

```
GET /health
200 { "ok": true, "model_loaded": true, "reference_loaded": true }
503 { "ok": false, "error": "..." }
```

Used by the main app's startup reachability check and by Docker/orchestration health probes.

```
POST /reload
200 { "ok": true }
```

Re-reads `reference/voice.wav` and recomputes speaker embeddings. Called by the main app after the reference-voice commit flow.

### Wrapper Implementation Notes

- `idiap/coqui-ai-TTS` Python package via pip (active fork; original `coqui-ai/TTS` is unmaintained)
- XTTS-v2 model: HuggingFace cache mounted as volume, first-run download, persistent cache
- Reference WAV at `backend/app/reference/voice.wav` (operator-provided from LibriTTS or similar permissively-licensed source, or uploaded via the Settings UI), mounted into the wrapper. The wrapper starts even without a voice: the model loads, `/health` reports `reference_loaded=false`, and `/generate` returns 503 until a voice is committed (via the UI, which writes voice.wav and calls `/reload`). Only a model-load failure exits the process.
- Pre-compute speaker embeddings at startup via `get_conditioning_latents()`, cache in memory, reuse every call
- Single uvicorn worker. Async endpoints. `asyncio.Lock` around the GPU inference call. `/health` (read-only) does not acquire the lock; `/reload` does (can't swap reference embeddings mid-inference). The lock means concurrent `/generate` requests queue cleanly at the inference boundary while `/health` stays responsive.
- Batch generation (no streaming)
- Language configurable via `TTS_LANGUAGE` env var, default `en`
- Generation parameters in `config.py`, tunable: temperature 0.65, length_penalty 1.0, repetition_penalty 2.0, top_k 50, top_p 0.85
- GPU pinned via Compose `device_ids: ['0']` by default; adjust to your hardware
- HF cache volume mounted at `/root/.cache/huggingface`
- Per-request timeout 120s, uvicorn worker timeout 300s
- GPU OOM handling: inference call wrapped in try/except for `torch.cuda.OutOfMemoryError`. On OOM, call `torch.cuda.empty_cache()`, return 500 with `details: "GPU OOM"`. The main app's `TTS_RETRY_COUNT` retries kick in upstream.
- Model load failure at startup: wrapper exits non-zero (same pattern as missing reference file). Container restart loop surfaces the problem instead of the wrapper serving 500s indefinitely.

Retries on `/generate` failures happen client-side in the main app (`TTS_RETRY_COUNT=3` with exponential backoff). The wrapper itself does not retry.

### XTTS License Note

XTTS-v2 model weights ship under the **Coqui Public Model License 1.0.0 (CPML)**. The license restricts use to non-commercial purposes. The paid commercial-license tier that Coqui formerly offered is gone -- Coqui AI shut down in January 2024 and no rights holder is currently selling commercial licenses.

For personal self-hosted use this is fine. README must document the license so operators know. Audicle does not redistribute model weights; the wrapper downloads them from Hugging Face on first run.

Coqui gates the first XTTS-v2 load behind an interactive y/n CPML agreement prompt. There is no TTY in a container, so the wrapper images set `COQUI_TOS_AGREED=1` to accept it non-interactively (deploying the wrapper is itself acceptance of the non-commercial CPML). Without it the model load hangs/fails at startup.

Code: the original `coqui-ai/TTS` Python package is unmaintained. The active fork is **`idiap/coqui-ai-TTS`** (MPL 2.0). Audicle's TTS wrapper installs from the Idiap fork. The model weights are still CPML regardless of which code fork loads them.

## Audio Pipeline

Per-episode flow after TTS generates per-chunk WAVs:

All inputs to the audio pipeline are 24000 Hz mono WAVs (XTTS-v2 native output, locked in the wrapper contract). The pipeline preserves this format through the trim, concat, and normalize steps, converting to 24000 Hz stereo MP3 only at the encode step.

1. **Trim silence** on each chunk WAV using torch-based detection. Threshold and buffer are tunable via `AUDIO_SILENCE_THRESHOLD` (default 0.003) and `AUDIO_SILENCE_BUFFER_MS` (default 5). Algorithm lifted from ebook2audiobook.
2. **Insert silence padding** between chunks. Generated as `torch.zeros(1, samples)` tensors at 24000 Hz. Duration tunable via `TTS_CHUNK_SILENCE_MS` (default 250).
3. **Concat** all chunk WAVs + silence tensors in Python via tensor append, write the combined WAV with `soundfile` (not `torchaudio.save`, which broke on the TorchCodec dependency change). Approach lifted from ebook2audiobook. ffmpeg takes over from step 4.
4. **Normalize** the concatenated WAV with the ebook2audiobook filter chain:
   ```
   agate=threshold=-25dB:ratio=1.4:attack=10:release=250,
   afftdn=nf=-70,
   acompressor=threshold=-20dB:ratio=2:attack=80:release=200:makeup=1dB,
   loudnorm=I=-14:TP=-3:LRA=7,
   # (single-pass loudnorm; the as-built filter omits linear=true / two-pass measurement)
   equalizer=f=150:t=q:w=2:g=1,
   equalizer=f=250:t=q:w=2:g=-3,
   equalizer=f=3000:t=q:w=2:g=2,
   equalizer=f=5500:t=q:w=2:g=-4,
   equalizer=f=9000:t=q:w=2:g=-2,
   highpass=f=63
   ```
   Output: -14 LUFS loudness target (podcast standard), voice-shaped EQ, denoise, gentle compression.
5. **Encode to MP3** via ffmpeg, store at `/data/media/{episode_id}.mp3`. Codec libmp3lame, sample rate `MP3_SAMPLE_RATE` (default 24000 to match XTTS native output), upmixed to stereo via `-ac 2` (mono content duplicated to L+R, matches podcast client expectations), bitrate `MP3_BITRATE` (default 128k). All tunable via env vars.
6. **Read final MP3 duration** via mutagen, store in DB for the RSS `<itunes:duration>` field.

Per-chunk durations come from the wrapper's `/generate` response and are tracked through the pipeline. Combined with the silence padding between chunks, they form the cumulative timeline used to generate VTT cue timestamps in the transcript stage.

Per-chunk WAVs and the concatenated WAV are deleted on both success and failure once the audio stage exits. No persistent debug artifacts.

## Chunking

Order in the pipeline: LLM cleanup -> pronunciation corrections (text substitution) -> chunk -> TTS. Chunking operates on the corrected prose so chunk boundaries don't fall mid-substitution.

Cleaned text is chunked for TTS using a hybrid strategy:

Paragraphs are detected by double-newline boundary (`\n\n`). The cleanup prompt instructs the LLM to separate paragraphs with blank lines; output without `\n\n` breaks falls through to sentence-level splitting from step 2.

1. Split on paragraph boundaries first
2. If a paragraph exceeds chunk size, split on sentence boundaries
3. If a sentence exceeds chunk size, split on commas or semicolons. Each fallback emits a structured WARN log record: `event=chunk_fallback_split`, `sentence_len_words`, `chunk_index`. Many of these in one article suggests a problematic sentence structure worth checking.
4. If even comma/semicolon splitting can't fit a sentence under the limit (no breakpoints, single long run of words), the chunk stage fails the job with a clear error including the offending sentence preview. Avoids silent content loss from forced word-boundary splits or truncation.

Target 180 words per chunk (`TTS_CHUNK_TARGET_WORDS`), hard max 220 words (`TTS_CHUNK_MAX_WORDS`).

Word counts use simple whitespace split (`text.split()`). A character-count safety cap (`TTS_CHUNK_MAX_CHARS`, default 1100) acts as a backstop in pathological cases like very long URLs or rare unsplit identifiers that wouldn't trigger the word-count limit.

Note: LLM cleanup is single-pass for any article length. The LLM doesn't chunk; it transforms input to output with similar length minus cruft.

If cleaned output is shorter than `MIN_CLEANUP_CHARS` (default 200), the chunk stage fails the job with a clear error. Mirrors the `MIN_EXTRACTION_CHARS` check at the extract stage. Catches cases where the LLM strips too aggressively or returns near-empty output.

Each resulting chunk is sent to the TTS wrapper via `POST /generate` with its 0-based `chunk_index`. See the TTS Wrapper section for the request/response contract.

## Pronunciation Corrections

Word-level pronunciation overrides applied as text substitutions between LLM cleanup and TTS chunking.

JSON dictionary at `backend/app/corrections/pronunciation.json`:

```json
{
  "kubectl": "kube control",
  "PostgreSQL": "post gres Q L"
}
```

### Substitution Mechanics

Matches use whole-word regex boundaries (`\b`), case-sensitive. `PostgreSQL` matches "PostgreSQL" wherever it appears but not "PostgreSQLite". `kubectl` doesn't match inside `kubectl-helper`. Case sensitivity is intentional: operators add multiple entries (`postgresql`, `Postgresql`, `PostgreSQL`) when they want every case to be corrected the same way.

Entries are applied longest-match-first. Sorting by key length descending before substitution prevents shorter keys from clobbering longer ones (so `kubectl` runs before `kube`, ensuring "kubectl" becomes "kube control" rather than "kube controlctl").

### Validation

PUT validates the submitted dictionary:

- Top-level must be a JSON object
- Each key: non-empty string, 1-100 chars, no leading/trailing whitespace
- Each value: non-empty string, 1-200 chars, no control characters
- Total entry count must not exceed `MAX_CORRECTIONS_ENTRIES`
- Keys are auto-escaped for regex special characters before compile, so operators can write `C++` or `node.js` without learning regex syntax

Invalid submissions return 400 with `details` listing which entries failed and why.

### API

`GET /api/v1/corrections` returns the current dictionary. `PUT /api/v1/corrections` accepts the entire dictionary as request body and replaces the stored file in one atomic write. UI keeps unsaved edits local and sends the whole dict on save.

Hard cap of `MAX_CORRECTIONS_ENTRIES` (default 500). PUT returns 400 if exceeded. Above this size, regex compile and full-text scan per article noticeably slow down the corrections stage.

Bind-mounted, editable on disk or via API. Both edit paths write to the same file (`backend/app/corrections/pronunciation.json`). Re-read on every job.

## Retention

- **Scope:** the daily sweep cleans up everything Audicle owns on disk and in the DB.
  - Expired episodes (audio, artwork, transcript, DB row): older than `RETENTION_DAYS` based on `pub_date`
  - Expired job rows: `done`/`failed` jobs older than `RETENTION_DAYS` that no live episode still references (queued/processing jobs are never reaped)
  - Expired migration backups: older than `MIGRATION_BACKUP_RETENTION_DAYS` (default 30)
  - (The reference `/test` flow stages and restores in place, so there are no `/data/tmp` reference temp files to sweep.)
- **Episode trigger:** N days from episode `pub_date` (not `created_at`), configurable via `RETENTION_DAYS` env var. Reprocessing an episode updates `pub_date` and therefore extends its retention window. This matches the intent of reprocess: it's an active signal that the episode matters.
- **Cleanup task:** runs daily at `RETENTION_SWEEP_HOUR_UTC`.
- **Deletion order:** for each expired episode, DB row deleted first, then media files (`{id}.mp3`, `{id}.jpg`). If file deletion fails, the DB row is already gone so the feed doesn't reference missing files. Failed file deletes are logged at WARN.
- **Orphan sweep:** the daily task also walks `/data/media/` and removes any file whose episode_id no longer exists in the `episodes` table. Catches files left behind by deletion failures or manual DB editing.
- **Manual:** `POST /api/v1/purge` removes all episodes. `POST /api/v1/purge?older_than_days=N` for partial cleanup. Both require a `confirm=true` query parameter to prevent accidents.

## RSS Feed

Podcasting 2.0 compliant. Generated on-demand from the episodes table; no static XML to maintain.

### Implementation Note: feedgen and PC2

The `feedgen` Python library does not natively support the Podcasting 2.0 `podcast:*` namespace. Two options:

1. Use `feedgen` for the standard RSS/iTunes elements, then post-process the generated XML to inject PC2 elements (MinusPod's approach: string-level XML construction for PC2 tags).
2. Hand-build the entire feed as templated XML, no `feedgen` dependency.

Recommended: option 1. Use `feedgen` for the well-trodden iTunes path, manually emit PC2 tags via XML string construction with proper escaping. Lift MinusPod's emission patterns (`_serialize_namespaced_element` and the channel-level PC2 block) as reference.

### Namespaces

```xml
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0"
     xmlns:atom="http://www.w3.org/2005/Atom">
```

### Channel-Level Fields

| Element | Value |
|---------|-------|
| `<title>` | `FEED_TITLE` env var |
| `<description>` | `FEED_DESCRIPTION` env var |
| `<language>` | `FEED_LANGUAGE` env var (default `en-us`) |
| `<link>` | `BASE_URL` |
| `<itunes:author>` | `FEED_AUTHOR` env var |
| `<itunes:owner>` | `FEED_AUTHOR` + `FEED_EMAIL` |
| `<itunes:category>` | `FEED_CATEGORY` env var (default `News`) |
| `<itunes:explicit>` | `FEED_EXPLICIT` env var (default `false`) |
| `<itunes:image href="...">` | `FEED_ARTWORK_URL` env var |
| `<image>` | Legacy RSS 2.0 image element. Contains `<url>`, `<title>`, `<link>`. `<url>` = `FEED_ARTWORK_URL`, `<title>` = `FEED_TITLE`, `<link>` = `BASE_URL`. |
| `<itunes:type>` | `episodic` (hardcoded) |
| `<pubDate>` | newest episode's `pub_date`, RFC 2822 format; omitted if no episodes |
| `<lastBuildDate>` | current UTC timestamp at feed generation, RFC 2822 format |
| `<atom:link rel="self">` | `{BASE_URL}/rss/rss.xml` |
| `<podcast:guid>` | Stable feed GUID, auto-generated UUIDv4 on first run and persisted to the `settings` table. Survives BASE_URL changes. |
| `<podcast:locked>` | `yes` (hardcoded), with `owner="{FEED_EMAIL}"` attribute |
| `<podcast:txt purpose="ai-content">` | a human-readable sentence declaring TTS-generated narration (honest AI-content declaration; the tag carries prose, not the literal `true`) |
| `<podcast:medium>` | `podcast` (hardcoded) |

### Per-Episode Fields

| Element | Value |
|---------|-------|
| `<title>` | Article title from Firecrawl metadata |
| `<description>` | HTML (CDATA-wrapped): title, author (if available), source link |
| `<itunes:summary>` | Plain text version of description |
| `<itunes:duration>` | mutagen-read duration formatted as `HH:MM:SS` (Apple-preferred) |
| `<itunes:image href="...">` | `{BASE_URL}/media/{id}.jpg` |
| `<enclosure url="..." type="audio/mpeg" length="...">` | `url={BASE_URL}/media/{id}.mp3`, `length` computed at feed-gen via `os.path.getsize(audio_path)` |
| `<pubDate>` | DB timestamp in RFC 2822 |
| `<guid isPermaLink="false">` | episode_id (12-char MD5 hash) |
| `<link>` | Original article URL |
| `<podcast:transcript url="..." type="text/vtt" language="en" rel="captions" />` | `{BASE_URL}/media/{id}.vtt` |

### Episode ID Strategy

- `episode_id = md5(article_url).hexdigest()[:12]`
- Deterministic: same URL produces same ID
- Used as DB primary key and in all media filenames
- GUID in RSS uses this ID

### Episode Ordering

Newest first. No feed-size limit; retention controls volume.

### HTTP Caching

`GET /rss/rss.xml` emits a `Last-Modified` header set to `MAX(episodes.updated_at)` (or the time the feed-level GUID was generated, whichever is later). Clients that send `If-Modified-Since` get a 304 Not Modified when nothing has changed. Saves bandwidth across the many podcast clients that poll feeds every 30-60 minutes.

`Cache-Control: max-age=300` (5 minutes) tells clients not to refetch more often than that. Tunable via `RSS_CACHE_MAX_AGE_SECONDS`.

### Duplicate Submission Behavior

- Default: 409 Conflict, returns existing episode_id
- With `reprocess=true`: deletes existing files, re-runs pipeline, updates DB row in place (same episode_id, new pub_date)

## Artwork Processing

- Supported source formats: JPEG, PNG, WebP, GIF (first frame), BMP, TIFF (anything Pillow reads). SVG is not supported and falls through to feed-art fallback.
- Source: article's `og:image` from Firecrawl metadata
- Download with timeout `ARTWORK_FETCH_TIMEOUT_SECONDS` (default 15s)
- Center-crop to square (lose edges, preserve resolution)
- Resize to `ARTWORK_SIZE_PX` (default 3000)
- Convert to JPG, quality `ARTWORK_JPG_QUALITY` (default 85), strip EXIF metadata (source EXIF can leak photographer location, camera serial, timestamps, original filename, etc.)
- Stored at `/data/media/{episode_id}.jpg`
- Fall back to feed-level artwork (`FEED_ARTWORK_URL`) on any of:
  - og:image missing from Firecrawl metadata
  - HTTP error (4xx, 5xx, connection refused)
  - Timeout (`ARTWORK_FETCH_TIMEOUT_SECONDS` exceeded)
  - Image format not supported by Pillow (e.g., SVG)
  - Source resolution below `ARTWORK_MIN_SOURCE_PX` on either axis (default 600). Smaller sources upscale poorly and produce blurry final artwork.
  - Pillow processing exception (corrupted file)
  - SSRF/abuse guards (the og:image URL comes from scraped HTML, not the operator): `blocked_scheme` (non-http/https), `blocked_host` (private/loopback/link-local/multicast IP after DNS resolution), `download_too_large` (body exceeds `ARTWORK_MAX_DOWNLOAD_BYTES`), `decompression_bomb` (Pillow pixel-limit), `atomic_write_failed`
- Each fallback logs the reason at WARN level (`event=artwork_fallback`, `reason=<...>`, `episode_id`, `source_url`) so operators can spot patterns.

### Feed-Level Artwork

The default feed artwork is the Audicle podcast cover from `branding/podcast-artwork-3000.png`, copied into `backend/app/assets/` at image build time. The app serves `/app/assets/` at the `/static/` route. `FEED_ARTWORK_URL` defaults to `{BASE_URL}/static/podcast-artwork-3000.png` but can be overridden via env var to point anywhere.

If the public-exposure layer only forwards `/rss/*` and `/media/*` (as in the reference Cloudflare Tunnel setup), either:

1. Extend the public path allowlist to include `/static/*`, or
2. Copy the artwork PNG into `/data/media/` at startup and reference it as `{BASE_URL}/media/feed-artwork.jpg`

Option 2 avoids expanding the public surface area.

## Transcript Generation

- Format: WebVTT with chunk-level timestamps (no word-level)
- Built from chunk text + per-chunk durations from the TTS wrapper's `/generate` responses
- Cumulative time includes `TTS_CHUNK_SILENCE_MS` padding between chunks (250ms default)
- Timestamps use VTT format: `HH:MM:SS.mmm` (dot separator before milliseconds)
- Cues are numbered for debuggability
- File starts with the required `WEBVTT` header line
- Special characters in cue text (`<`, `>`, `&`) are escaped per VTT spec
- Stored in `transcript_vtt` column on episodes table
- Served on-demand via `/media/{episode_id}.vtt` route, content-type `text/vtt`
- `Cache-Control: public, max-age=86400` (transcripts are immutable once an episode is finalized)
- Referenced from RSS via `<podcast:transcript>` per episode (language attribute on that tag, not inside the VTT)

### Example

```
WEBVTT

1
00:00:00.000 --> 00:00:12.450
When the cache hit rate dropped to 47 percent, the team investigated.

2
00:00:12.700 --> 00:00:24.800
They found that the TTL of 300 seconds was too aggressive.
```

The 250ms gap between cue 1's end (12.450) and cue 2's start (12.700) is the silence padding inserted in the audio pipeline.

## Public Exposure

Audicle uses a two-domain split for clean separation between admin and public surface:

- **UI / admin domain** (example: `audicle.example.com`) -- serves the React UI at `/` and the admin API at `/api/v1/*`. Restricted to LAN or private network at the exposure layer. Operator-only.
- **Public feed domain** (example: `audifeed.example.com`) -- serves `/rss/*` and `/media/*` only. Internet-accessible. Podcast clients hit this domain.

Both domains route to the same FastAPI app and the same container. The exposure layer is responsible for:

1. Mapping each domain to the right path set
2. Blocking wrong-path requests (e.g., `/api/v1/*` from the public feed domain)
3. HTTPS termination on both (the app speaks HTTP internally)

`BASE_URL` env var must be set to the **public feed domain** since RSS enclosures, `<atom:link rel="self">`, and per-episode media references are built from it. UI access uses whatever domain loads the app and doesn't drive feed URLs.

`UI_BASE_URL` env var is the **admin UI domain**, used by anything that emits links destined for the UI (future webhooks, notification emails, etc.). Falls back to `BASE_URL` if unset, matching MinusPod's pattern.

`/health` is for container orchestration probes, the app's own startup reachability checks, and operator monitoring. Not exposed on either domain to anything outside the LAN.

The frontend UI's PWA capabilities (install-to-homescreen, service worker registration) require HTTPS. Browsers exempt `localhost` from this rule, so dev works without TLS, but production UI access over plain HTTP loses PWA features. Most exposure layers provide TLS termination automatically.

The exposure layer must pass Audicle's response headers through unchanged. The feed depends on `Last-Modified` and `Cache-Control` on `/rss/rss.xml` for the conditional-GET flow that saves bandwidth across polling clients. Media routes set `Content-Type` and `Content-Length` that podcast clients rely on. Proxies and CDN edges sometimes strip or override these headers by default; verify with `curl -I` against the public URL after setup.

Common options for either domain:

| Option | Notes |
|--------|-------|
| Cloudflare Tunnel | Reference setup. Path filtering at ingress + WAF rules. Zero exposed ports on the host. |
| Tailscale Funnel | Easiest if already on Tailscale. Per-path control via `tailscale serve` config. |
| Reverse proxy (Nginx, Caddy, Traefik) | Standard pattern. TLS via Let's Encrypt. Use the location/path matcher to restrict. |
| ngrok / similar | Quick testing, not production. |
| Direct (firewall + reverse proxy on the same host) | Most control, most setup work. |

### Reference: Cloudflare Tunnel + WAF

The example below restricts the public surface to `/rss/*` and `/media/*` and further restricts the User-Agent to Pocket Casts. This is the strictest of the common options because it pairs path filtering with client identity. Other deployment layers can match the path-filter half without the UA restriction.

**Tunnel ingress rules:**

```yaml
ingress:
  - hostname: feed.yourdomain.com
    path: ^/(rss/.*|media/.*)$
    service: http://app:8000
  - service: http_status:404
```

**WAF custom rule (block action):**

```
NOT (
  (http.request.method eq "GET" or http.request.method eq "HEAD")
  and
  (http.request.uri.path eq "/rss/rss.xml"
   or http.request.uri.path matches "^/media/.*\.(mp3|jpg|vtt)$")
  and
  http.user_agent contains "Pocket Casts"
)
```

HEAD must be allowed because podcast clients send HEAD before GET to check content-length before downloading audio.

The User-Agent lock to "Pocket Casts" is optional. Remove that clause to allow other podcast clients. The path filter is the load-bearing protection.

## GPU Configuration

XTTS-v2 requires a CUDA GPU with roughly 4-6GB VRAM (model + speaker embeddings + per-inference overhead). The `tts-wrapper` container is the only Audicle component that needs GPU access.

The compose file pins the wrapper to a single device via Docker's `device_ids`:

```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          device_ids: ['0']
          capabilities: [gpu]
```

Operators with multiple GPUs may want to pin Audicle to a specific one so it doesn't fight with other GPU workloads (Ollama, other inference services). Adjust `device_ids` accordingly. Pin by GPU UUID (`nvidia-smi -L`) rather than index if PCIe enumeration is unstable across reboots.

For LLM endpoints (Ollama or otherwise): Audicle calls the LLM over HTTP and doesn't care where it runs. GPU placement for the LLM is the operator's concern.

### Requirements

- NVIDIA GPU with CUDA 11.8+ support (matches PyTorch's supported wheel range)
- NVIDIA Container Toolkit installed on the host
- 4-6GB VRAM peak depending on chunk length. Plan for 6GB headroom to avoid OOM under longer chunks.
- Linux host (Docker Desktop's GPU support is limited)

The TTS wrapper Dockerfile uses an official PyTorch base image (e.g. `pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime`) so CUDA + cuDNN are preconfigured. Operators don't manage CUDA install; the base image handles it.

### CPU Fallback

`TTS_DEVICE=cuda` is the default. Set `TTS_DEVICE=cpu` to run inference on CPU instead. Lets operators without a CUDA GPU try Audicle, with caveats:

- Synthesis is ~5-10x slower (a 5-minute article may take 25+ minutes)
- The `nvidia` device reservation block in compose must be removed
- Build the wrapper from `tts-wrapper/Dockerfile.cpu`, which uses a plain `python:3.11-slim` base and installs the CPU-only torch wheel from PyTorch's CPU index (there is no `pytorch/pytorch:*-cpu` tag), avoiding the unused CUDA libraries

CPU mode is supported but not recommended for regular use.

### Model Weights

XTTS-v2 weights (~2GB) are downloaded from Hugging Face on first run. The compose file mounts a named volume at `/root/.cache/huggingface` inside the wrapper so weights persist across container rebuilds. Without this mount, every rebuild re-downloads.

```yaml
tts-wrapper:
  volumes:
    - hf_cache:/root/.cache/huggingface

volumes:
  hf_cache:
```

First-run download takes a few minutes depending on connection. All subsequent restarts load from local cache instantly.

## Configuration (env vars)

All tunable values in `backend/app/config.py` via Pydantic `BaseSettings`. Env vars override defaults. Required vars fail fast at startup if missing.

### Required (no defaults)

```
BASE_URL=https://audifeed.example.com
UI_BASE_URL=https://audicle.example.com
DATA_DIR=/data
FIRECRAWL_URL=http://firecrawl:3002
TTS_URL=http://tts-wrapper:8000
LLM_PROVIDER=openai-compatible    # or "anthropic"
LLM_MODEL=                        # required, no default (example: qwen2.5:14b)
FEED_TITLE=
FEED_DESCRIPTION=
FEED_AUTHOR=
FEED_EMAIL=
FEED_ARTWORK_URL=
```

### Required (conditional)

```
OPENAI_BASE_URL=http://host.docker.internal:11434/v1   # if LLM_PROVIDER=openai-compatible
OPENAI_API_KEY=not-needed                              # if LLM_PROVIDER=openai-compatible
ANTHROPIC_API_KEY=                                     # if LLM_PROVIDER=anthropic
```

### Optional (deployment-specific)

```
CF_TUNNEL_TOKEN=                  # compose-only (consumed by a cloudflared service); the app's
                                  # Pydantic settings use extra="forbid", so this is NOT read by the app
```

### Tunable (with defaults)

```
# Feed
FEED_LANGUAGE=en-us
FEED_CATEGORY=News
FEED_EXPLICIT=false

# Extraction
FIRECRAWL_RETRY_COUNT=3
FIRECRAWL_BACKOFF_BASE_SECONDS=1
FIRECRAWL_TIMEOUT_SECONDS=30
MIN_EXTRACTION_CHARS=500
MIN_CLEANUP_CHARS=200

# LLM
LLM_TEMPERATURE=0.7
LLM_MAX_TOKENS=4000
LLM_TIMEOUT_SECONDS=300
LLM_RETRY_COUNT=3

# TTS
TTS_LANGUAGE=en
TTS_DEVICE=cuda                  # or "cpu" for CPU-only mode (much slower)
XTTS_TEMPERATURE=0.65
XTTS_LENGTH_PENALTY=1.0
XTTS_REPETITION_PENALTY=2.0
XTTS_TOP_K=50
XTTS_TOP_P=0.85
TTS_RETRY_COUNT=3
TTS_HTTP_TIMEOUT_SECONDS=120
TTS_REACHABILITY_GRACE_SECONDS=60      # startup probe retry window for the wrapper
TTS_REACHABILITY_PROBE_TIMEOUT=10

# Chunking
TTS_CHUNK_TARGET_WORDS=180
TTS_CHUNK_MAX_WORDS=220
TTS_CHUNK_MAX_CHARS=1100
TTS_CHUNK_SILENCE_MS=250

# Audio
LOUDNORM_TARGET_LUFS=-14
LOUDNORM_TRUE_PEAK_DB=-3
LOUDNORM_LRA=7
AUDIO_SILENCE_THRESHOLD=0.003
AUDIO_SILENCE_BUFFER_MS=5
MP3_BITRATE=128k
MP3_SAMPLE_RATE=24000
MP3_CHANNELS=2

# Timeouts
TTS_HTTP_TIMEOUT_SECONDS=120
JOB_TIMEOUT_SECONDS=1800

# Queue
QUEUE_POLL_INTERVAL_SECONDS=2

# HTTP
WEB_WORKERS=2

# Limits
MAX_PROMPT_LENGTH_BYTES=10240
MAX_CORRECTIONS_ENTRIES=500
# (reference upload cap is a hardcoded 5MB in reference.py, not an env var)

# Logging
LOG_LEVEL=INFO
LOG_FORMAT=json    # json | text (json default for Loki ingest)

# Retention
RETENTION_DAYS=90
RETENTION_SWEEP_HOUR_UTC=7
MIGRATION_BACKUP_RETENTION_DAYS=30

# RSS
RSS_CACHE_MAX_AGE_SECONDS=300

# Auth (off by default)
AUTH_ENABLED=false                # master switch
ADMIN_USERNAME=admin              # required when AUTH_ENABLED=true
ADMIN_PASSWORD_HASH=              # required when AUTH_ENABLED=true; bcrypt hash
SESSION_SECRET_KEY=               # required when AUTH_ENABLED=true; ephemeral per-process when off
SESSION_COOKIE_SECURE=false       # set true once HTTPS fronts the app
SESSION_COOKIE_MAX_AGE_SECONDS=1209600   # 14 days
LOCKOUT_MAX_FAILED_ATTEMPTS=5     # failed login attempts before lockout
LOCKOUT_WINDOW_SECONDS=900        # 15-minute lockout window
LOGIN_RATE_LIMIT=10/minute        # advisory; slowapi decorator value is hardcoded

# Artwork
ARTWORK_SIZE_PX=3000
ARTWORK_JPG_QUALITY=85
ARTWORK_FETCH_TIMEOUT_SECONDS=15
ARTWORK_MIN_SOURCE_PX=600
ARTWORK_MAX_DOWNLOAD_BYTES=26214400   # 25MB SSRF/decompression-bomb download cap

# CORS
CORS_ORIGINS=                     # comma-separated origin list; empty = permissive within container/LAN
```

### UI-Editable Subset

The following env var keys can be overridden at runtime via the Settings UI, which writes to the `runtime_settings` DB table. Config resolution per job: code default -> env -> DB override (last wins).

The implemented allowlist (`services/runtime_settings.py` `ALLOWED_KEYS`) is the
operator-tunable subset. API keys are included but masked on read (see
`MASKED_KEYS`); infrastructure paths (`DATA_DIR`, `TTS_URL`) and the XTTS
generation params stay env-only:

```
# LLM group (OPENAI_API_KEY / ANTHROPIC_API_KEY are masked on read)
LLM_PROVIDER
LLM_MODEL
OPENAI_BASE_URL
OPENAI_API_KEY
ANTHROPIC_API_KEY
LLM_TEMPERATURE
LLM_MAX_TOKENS
LLM_TIMEOUT_SECONDS
LLM_RETRY_COUNT

# Feed group
FEED_TITLE
FEED_DESCRIPTION
FEED_AUTHOR
FEED_EMAIL
FEED_LANGUAGE
FEED_CATEGORY
FEED_EXPLICIT
FEED_ARTWORK_URL

# TTS / chunking group
TTS_CHUNK_TARGET_WORDS
TTS_CHUNK_MAX_WORDS
TTS_CHUNK_SILENCE_MS

# Cleanup group
MIN_CLEANUP_CHARS
MAX_PROMPT_LENGTH_BYTES

# Retention group
RETENTION_DAYS

# RSS group
RSS_CACHE_MAX_AGE_SECONDS
```

`TTS_URL`, `DATA_DIR`, and the XTTS generation params are NOT in the allowlist
(infrastructure or restart-affecting); they stay env-only. The worker applies
the overlay per job, so allowlisted edits take effect on the next submission.

Plus the cleanup prompt (`PUT /api/v1/prompt` writes to `prompts/script.txt` directly) and pronunciation corrections (`PUT /api/v1/corrections` writes to `corrections/pronunciation.json` directly). These aren't env vars; they're files with their own endpoints.

The allowlist lives in code. Unknown keys posted by the UI are rejected. All other env vars in the Tunable block are env-only and require a container restart to change.

## Failure Handling and Observability

### Per-Stage Tracking

Every job row has a `stage` column. When status flips to `failed`, `stage` pinpoints where the failure occurred.

### Structured Logging

Stdlib `logging` with a custom `JSONFormatter` lifted from MinusPod. Output mode controlled by `LOG_FORMAT` env var (`json` default, `text` for local readability). Level controlled by `LOG_LEVEL`.

Designed for Loki ingestion via Promtail or the Docker Loki driver. To avoid label cardinality explosion in Loki:

- **Indexed labels (low cardinality):** `level`, `stage`, `status`, `service`
- **Body fields (high cardinality):** `job_id`, `episode_id`, `url`, `message`, `exception`

Context propagation uses `contextvars` (FastAPI equivalent of Flask's `g`). A context filter pulls the current `job_id` onto every log record emitted inside a job's processing scope, so individual log calls don't need to pass it explicitly.

Every record includes `timestamp` (ISO 8601), `level`, `logger`, `hostname`, `pid`, `message`, plus context fields when present.

### Version Logging

`backend/app/version.py` holds `__version__` as the single source of truth. Imported by:

- `main.py` lifespan -- logs `Audicle starting` with version, Python version, hostname, PID at startup
- FastAPI app metadata (`version=__version__`)
- `pyproject.toml` build metadata (kept in sync manually or via build script)
- `GET /health` endpoint response

### Health Endpoints

Three routes, standard Kubernetes-style:

- **`GET /health/live`** -- process is alive. No dependency checks. Returns 200 + minimal body (`{"ok": true, "version": "0.1.0"}`). Used by orchestrators for restart decisions.
- **`GET /health/ready`** -- dependencies reachable, ready to serve traffic. Returns full body (see below). 503 if any subsystem fails. Used by orchestrators for traffic-routing decisions.
- **`GET /health`** -- alias for `/health/ready`. Kept for backward compatibility and casual use.

All three are LAN-only.

Full body shape for `/health/ready` and `/health`:

```json
{
  "ok": true,
  "version": "0.1.0",
  "uptime_seconds": 12345,
  "components": {
    "app": "0.1.0",
    "python": "3.13.1",
    "tts_wrapper": {
      "version": "0.1.0",
      "torch": "2.4.0+cu124",
      "coqui_tts": "0.22.0",
      "device": "cuda",
      "model_loaded": true
    },
    "ffmpeg": "6.1.1",
    "firecrawl": {
      "url": "http://firecrawl:3002",
      "reachable": true
    },
    "llm": {
      "provider": "openai-compatible",
      "model": "qwen2.5:14b",
      "base_url": "http://host.docker.internal:11434/v1",
      "reachable": true
    }
  },
  "checks": {
    "db": "ok",
    "tts_wrapper": "ok",
    "firecrawl": "ok",
    "llm": "ok"
  }
}
```

The main app's `/health/ready` queries the wrapper's `/health` to populate `components.tts_wrapper`. Wrapper-side `/health` reports its own component-level detail (torch version, coqui-tts version, device, model load status).

### Crash Recovery

On startup, any job left in `processing` (from a crash mid-pipeline) is reset to `queued` so the worker picks it up again. Detailed in the Pipeline section under Crash Recovery. The reset is logged at `stage=startup`.

### Error Surfacing

- `jobs.error` column holds exception message
- `GET /api/v1/status/{job_id}` returns error and stage
- `GET /api/v1/jobs?status=failed` lists all failed jobs with filters

### Sanity Checks

- Extraction: minimum length threshold (default 500 chars). Below this, fail with clear message.
- LLM output: validate plain text (no markdown leakage, no preamble). Optional structural validation.
- TTS: per-chunk duration must be non-zero. Empty WAV indicates upstream failure.

### Startup Reachability Checks

Before the queue worker starts processing, the app probes each required external endpoint and logs the result. Failures cause the app to exit non-zero so the container restart loop surfaces the problem instead of silently failing every job.

Checks:

| Endpoint | Method | Expected | On failure |
|----------|--------|----------|------------|
| Firecrawl `FIRECRAWL_URL` | GET `/` or `/health` (per Firecrawl version) | HTTP 200 within 5s | Log + exit non-zero |
| LLM endpoint (OpenAI-compatible) | GET `OPENAI_BASE_URL/models` | HTTP 200, JSON listing models | Log + exit non-zero |
| LLM endpoint (Anthropic) | Skip; Anthropic API has no cheap health check | n/a | Validate API key format at startup only |
| TTS wrapper `TTS_URL` | GET `/health` | HTTP 200 within 10s (wrapper may still be loading model) | Log + retry for up to 60s, then exit non-zero |

The TTS wrapper gets a longer grace period because XTTS-v2 takes 10-30 seconds to load weights and pre-compute speaker embeddings at startup. If the app boots faster than the wrapper, polling avoids a startup race.

These checks are logged as structured records with `stage=startup` and the endpoint name so they're easy to find in Loki when something doesn't come up.

## Docker Compose

```yaml
services:
  app:
    image: ghcr.io/ttlequals0/audicle:latest    # bundles built frontend UI
    # entrypoint.sh starts uvicorn (HTTP) and python -m app.worker (queue), supervises both
    ports:
      - "8000:8000"    # bind to a LAN IP or localhost if admin should not be world-reachable
    env_file: .env
    extra_hosts:
      - "host.docker.internal:host-gateway"   # lets the app reach host-installed Ollama on Linux
    volumes:
      - ./data:/data
      # Image copies the package to /app/app, so the editable mounts target /app/app/*
      - ./backend/app/prompts:/app/app/prompts
      - ./backend/app/corrections:/app/app/corrections
      - ./backend/app/reference:/app/app/reference
    depends_on:
      tts-wrapper:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health/live"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    restart: unless-stopped

  tts-wrapper:
    image: ghcr.io/ttlequals0/audicle-tts:latest
    environment:
      - TTS_LANGUAGE=${TTS_LANGUAGE:-en}
      - TTS_DEVICE=${TTS_DEVICE:-cuda}
    volumes:
      - ./data:/data
      - ./backend/app/reference:/app/reference:ro
      - hf_cache:/root/.cache/huggingface
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 120s    # model load takes time on first start
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ['0']
              capabilities: [gpu]
    restart: unless-stopped

  # Optional: public exposure. Swap or remove based on your setup.
  cloudflared:
    image: cloudflare/cloudflared:latest
    command: tunnel --no-autoupdate run --token ${CF_TUNNEL_TOKEN}
    restart: unless-stopped

volumes:
  hf_cache:
```

Notes:

- The `app` container runs two processes via `entrypoint.sh`: `uvicorn --workers ${WEB_WORKERS:-2}` for HTTP and `python -m app.worker` for the SQLite-backed queue. The entrypoint supervises both; if either exits, the container exits and the restart policy brings it back clean. Both read the same `/data` (WAL mode).
- For CPU-only deployment, set `TTS_DEVICE=cpu` and remove the `deploy.resources` block from `tts-wrapper`.
- The `app` port binding defaults to all interfaces. Bind to a LAN IP (`"192.168.x.x:8000:8000"`) or localhost if the admin surface should not be world-reachable; the two-domain exposure layer handles public access to the feed.

## Build Order

Phased approach so each phase produces a working, testable slice. Phases are ordered by dependency: every phase builds only on endpoints and behavior established in earlier phases. No timeline estimates; sequence over schedule. Each phase ends with a manual or automated check before moving on.

### Phase 1: Project Scaffold

- Repo structure per layout above
- FastAPI app skeleton with `/health/live`, `/health/ready`, `/health`
- `app/worker.py` queue process skeleton (loops, polls DB, does nothing yet)
- `entrypoint.sh` starting both uvicorn and the worker, supervising both
- Pydantic `BaseSettings` config, `version.py`
- SQLite schema and idempotent startup migrations, WAL mode
- Dockerfile (multi-stage shell, frontend stage stubbed for now)
- Docker Compose with the app service (entrypoint runs both processes)
- Structured logging (stdlib + JSONFormatter), version logged at startup

### Phase 2: Extraction

- Firecrawl client with retry and backoff (tenacity)
- `POST /api/v1/submit` endpoint creating job rows (201, episode_id = MD5(url)[:12], 409 on duplicate)
- `GET /api/v1/status/{job_id}` endpoint
- Queue worker process picking up queued jobs, single in-flight, polling interval
- Stage tracking written to DB at each transition
- Job timeout wrapper (`asyncio.wait_for`)
- Crash recovery: reset `processing` rows to `queued` on worker startup
- Startup reachability checks (Firecrawl now; LLM and TTS added as those phases land)
- Manual end-to-end test: submit URL, see `extract` stage complete, raw markdown stored or logged

### Phase 3: LLM Cleanup

- Multi-provider LLM client (anthropic + openai-compatible)
- Prompt file loading and re-read per job
- `GET/PUT /api/v1/prompt` endpoints
- Pronunciation corrections file loading
- `GET/PUT /api/v1/corrections` endpoints
- Pipeline runs through cleanup stage, stores result in temp field for inspection

### Phase 4: TTS Wrapper

- Separate `tts-wrapper/` container, PyTorch official base image
- Coqui TTS install (idiap fork), model download via HF cache mount
- Reference voice loading, embedding pre-computation at startup
- Exit non-zero if `voice.wav` missing or model load fails
- `POST /generate` (with `chunk_index`), `GET /health`, `POST /reload` endpoints
- Async lock around inference; `/health` stays responsive
- GPU OOM handling (empty_cache + 500); `TTS_DEVICE=cuda|cpu` support
- LLM and TTS reachability checks wired into the app startup
- GPU pinning verified with `nvidia-smi`
- Manual test: send chunk text, get WAV back; verify CPU fallback path

### Phase 5: Chunking and Audio Pipeline

- `MIN_CLEANUP_CHARS` guard (fail job if cleanup output too short)
- Hybrid chunker (paragraphs -> sentences -> commas), fallback WARN logging, hard abort with sentence preview if no breakpoint fits
- Per-chunk TTS calls with `chunk_index`, client-side retry
- Silence trim (torch), silence padding (torch zeros), concat (tensor append + torchaudio)
- ffmpeg normalization filter chain (loudnorm, EQ, denoise, compress)
- MP3 encode (libmp3lame, 24000 Hz, stereo upmix, 128k)
- Intermediate file cleanup on success and failure
- Per-chunk duration tracking for transcripts

### Phase 6: Artwork and Transcripts

- Firecrawl metadata parsing for og:image
- Download, center-crop, resize, JPG conversion with Pillow
- Fallback to feed artwork on failure
- VTT generation from chunks + durations
- Transcript stored in DB

### Phase 7: RSS Feed and Media Serving

- feedgen-based generator pulling from episodes table
- Channel fields from env vars: title, description, author, owner, category, explicit, image
- `pubDate` (newest episode), `lastBuildDate` (now), legacy `<image>` element
- `podcast:guid` generated on first run, persisted to `settings`; `podcast:txt purpose="ai-content"`=true; `podcast:locked`
- PC2 namespace and tags via string-level XML construction (MinusPod pattern)
- Per-episode fields: enclosure (length via getsize), `itunes:duration` as HH:MM:SS, `podcast:transcript`
- HTTP caching: `Last-Modified` + `Cache-Control` on `/rss/rss.xml`, conditional GET 304 support
- `GET /rss/rss.xml` endpoint
- `GET /media/{id}.mp3`, `GET /media/{id}.jpg` static handlers
- `GET /media/{id}.vtt` DB-backed handler with `Cache-Control: max-age=86400`

### Phase 8: Retention and Purge

- Daily background sweep for expired episodes
- File and DB cleanup
- `POST /api/v1/purge` endpoint with confirmation
- `POST /api/v1/purge?older_than_days=N` partial purge

### Phase 9: Authentication

- `settings` and `auth_lockout` tables
- `services/auth.py`: bcrypt hashing, session management
- `services/csrf.py`: token generation and validation
- `/api/v1/auth/*` endpoints
- slowapi rate limiting on login
- SessionMiddleware wiring in `main.py`
- New env vars: `SESSION_SECRET_KEY`, `SESSION_COOKIE_SECURE`, lockout thresholds
- Smoke test: enable auth, log in, log out, hit lockout, recover after timeout

### Phase 10: Runtime Settings

- `runtime_settings` table
- Config resolution chain (default -> env -> DB override), re-read per job
- `GET/PUT /api/v1/settings` with allowlist enforcement
- `GET /api/v1/episodes` (list, pagination, X-Total-Count) and `DELETE /api/v1/episodes/{id}`
- Reference audio endpoints: `/reference/preview`, `/reference/test`, `/reference/commit`
- All admin endpoints the UI will consume now exist and are stable

### Phase 11: Web UI

- Vite + React + TypeScript + Tailwind + React Query + React Router scaffolding in `frontend/`
- Three routes: Home, Feed (with copy-feed-URL button), Settings (5 groups + system info)
- Design system per branding tokens (Satoshi + JetBrains Mono, mobile-first)
- React Query hooks for `/api/v1/*` endpoints
- Login screen wired to auth status; CSRF token handling in mutations
- Pull-to-refresh on Feed
- Reference audio widget (upload, test, commit)
- `vite-plugin-pwa` for `manifest.json` and service worker
- Dockerfile frontend stage activated: Node build -> copy `dist/` into Python runtime image
- FastAPI `StaticFiles` mount at `/` serving the built UI

### Phase 12: Public Exposure

- Pick an exposure layer (Cloudflare Tunnel, Tailscale Funnel, reverse proxy, etc.)
- Restrict public paths to `/rss/*` and `/media/*`
- Optionally restrict by client User-Agent (reference setup uses Pocket Casts)
- Smoke test: subscribe in a podcast client from outside the LAN, confirm playback

### Phase 13: Polish

- `GET /api/v1/jobs?status=failed` endpoint with pagination
- Full `/health/ready` component-version aggregation (query wrapper `/health`, read ffmpeg version, LLM/Firecrawl reachability detail)
- Orphan media sweep in the retention task
- README documenting all env vars, valid iTunes categories, XTTS license caveat, Coqui TTS 3.13 install note
- `openapi.yaml` committed, `scripts/dump_openapi.py` to regenerate
- `reference/README.md` (voice clip specs and sources)
- LICENSE file (MIT)
- Smoke tests in `backend/tests/`

## Dependencies Lifted from Other Projects

| Source | What | Where |
|--------|------|-------|
| [MinusPod](https://github.com/ttlequals0/MinusPod) | Single-container pattern, SQLite-as-queue, crash recovery | Whole architecture |
| [MinusPod](https://github.com/ttlequals0/MinusPod) | Multi-provider LLM abstraction (anthropic + openai-compatible) | `services/llm.py` |
| [MinusPod](https://github.com/ttlequals0/MinusPod) | Episode ID strategy (MD5[:12]) | `services/pipeline.py` |
| [MinusPod](https://github.com/ttlequals0/MinusPod) | Pronunciation corrections dictionary pattern (inverse direction) | `services/corrections.py` |
| [MinusPod](https://github.com/ttlequals0/MinusPod) | Stage tracking on jobs, structured logging with job_id, JSONFormatter | `worker.py` + `services/jobs.py`, `utils/logging.py` |
| [MinusPod](https://github.com/ttlequals0/MinusPod) | PC2 namespace emission patterns, transcript-in-DB storage | `services/feed.py` |
| [MinusPod](https://github.com/ttlequals0/MinusPod) | Optional password auth: sessions, CSRF, IP lockout, rate limiting | `services/auth.py`, `services/csrf.py` |
| [MinusPod](https://github.com/ttlequals0/MinusPod) | Application-managed timestamps, idempotent startup migrations | `core/database.py` |
| [MinusPod](https://github.com/ttlequals0/MinusPod) | `BASE_URL` / `UI_BASE_URL` split | `config.py` |
| [ebook2audiobook](https://github.com/DrewThomasson/ebook2audiobook) | ffmpeg audio normalization filter chain (loudnorm + EQ) | `services/audio.py` |
| [ebook2audiobook](https://github.com/DrewThomasson/ebook2audiobook) | Silence trim algorithm with torch | `services/audio.py` |
| [ebook2audiobook](https://github.com/DrewThomasson/ebook2audiobook) | mutagen for duration metadata | `services/audio.py` |
| [python-webapp-template](https://github.com/sjafferali/python-webapp-template) | Project layout, lifespan pattern, API versioning | Whole structure |
| [podcastfy](https://github.com/souzatharsis/podcastfy) | Section-aware prompt enhancement pattern (informed prompt design, not directly used) | `prompts/script.txt` |

## Other Repos Evaluated

These repos were reviewed during planning but their code/approach was not adopted. Documented here so the team understands what was considered and rejected.

| Source | Reason for review | Outcome |
|--------|-------------------|---------|
| [Nari Labs Dia](https://github.com/nari-labs/dia) | Initial TTS candidate (multi-speaker dialogue) | Rejected after pivot to single-narrator format; Dia is optimized for two-speaker dialogue |
| [Nari Labs Dia2](https://github.com/nari-labs/dia2) | Streaming Dia variant | Rejected: less mature, stability warnings in docs, streaming not needed for offline batch use |
| [tts-bench](https://github.com/5uck1ess/tts-bench) | TTS benchmark data | Informed quality/license/speed reasoning; benchmark itself not used in runtime |
| [Firecrawl](https://github.com/firecrawl/firecrawl) | Article extraction (self-hosted instance) | Adopted as external dependency, not lifted code |
| [Coqui TTS (idiap fork)](https://github.com/idiap/coqui-ai-TTS) | XTTS-v2 implementation | Adopted via pip (idiap fork; original coqui-ai/TTS unmaintained); CPML weights license documented |

## License Notes

- **XTTS-v2 weights:** Coqui Public Model License 1.0.0 (CPML), non-commercial. Coqui AI shut down in January 2024; no commercial license tier exists anymore. Personal self-hosted use is fine. Audicle does not redistribute weights; the wrapper downloads them from Hugging Face on first run. README documents the constraint.
- **Coqui TTS code:** the original `coqui-ai/TTS` is unmaintained; Audicle installs the active `idiap/coqui-ai-TTS` fork (MPL 2.0). Weights remain CPML regardless of code fork.
- **This project:** MIT. The Audicle name and logo are reserved (see `branding/README.md`); MIT covers the code, not the brand.

## Things Explicitly Out of Scope

- Multiple voices or voice rotation
- Two-speaker dialogue
- Word-level transcript timestamps
- OPML import/export
- Multi-user accounts (optional single-password auth exists; no per-user accounts, roles, or multi-tenancy)
- Public submission endpoint
- Auto-discovery (RSS reading inputs)
- Vision model for image description (deferred; alt text only)
- Playwright extraction fallback (deferred; Firecrawl handles it)
- arq/Redis job queue (deferred; SQLite-as-queue is sufficient)
- Streaming TTS (not needed; batch is fine)
- Multiple TTS providers (XTTS-v2 only)
- Sentence-level transcript granularity beyond chunk boundaries
- Prometheus `/metrics` endpoint (deferred; structured logs to Loki cover observability)

## Known Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Firecrawl downtime breaks pipeline | 3 retries with backoff; failure logged; user can resubmit |
| XTTS produces inconsistent voice quality | Pre-computed embeddings, fixed reference clip; tunable generation params |
| LLM adds hallucinated content despite "cleanup only" framing | Prompt rules constrain the LLM to remove/transform/normalize only, leaving unmatched prose intact; output spot-checked during early use |
| Article extraction returns empty or near-empty content | `MIN_EXTRACTION_CHARS` and `MIN_CLEANUP_CHARS` thresholds fail fast with clear errors |
| GPU OOM under load | Single in-flight job, no concurrent TTS calls; wrapper catches OOM, clears cache, returns 500, client retries |
| Public exposure layer locks out a needed client | Reference WAF rule is restrictive by design; relax or remove the User-Agent clause if multiple clients are needed |
| User submits same URL twice unintentionally | 409 by default; `reprocess=true` required to overwrite |
| Disk fills up | Retention sweep deletes expired episodes daily |
| Container restart loses in-flight job | Crash recovery resets `processing` to `queued` on startup |
| Pronunciation issues with new technical terms | Corrections JSON dictionary, editable via API |
| TTS quality degrades on long sentences | Chunk-size limits + comma/semicolon fallback split; hard abort with clear error if unsplittable |
| Schema migration corrupts or loses data | Idempotent migrations, DB backup before each run, file lock, no destructive column drops |
| One of the two app processes dies silently | Entrypoint supervises both; if either exits the container exits and restart policy brings it back clean |
| Stale feed GUID after domain move | GUID is a persisted UUIDv4 independent of BASE_URL, survives domain changes |
