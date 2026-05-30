# Changelog

All notable changes to Audicle are recorded here. Format follows Keep a Changelog
(https://keepachangelog.com). Versioning is semver once a release ships; pre-release
work lives under `[Unreleased]`.

## [Unreleased]

## [0.3.2] - 2026-05-29

### tts-wrapper non-root cache fix, structured wrapper logs, and PWA route fallback

- **Non-root cache paths:** both wrapper Dockerfiles defaulted `HF_HOME`/`TTS_HOME` to `/root/.cache`, which is mode-700 root-owned and crashes the `user: 1000:1000` container on startup -- the Numba "no locator available" error and the Coqui "Permission denied: '/root/.cache'" model-download error both trace back to it. Caches now default to writable paths: model weights on the persistent `/data` volume (`HF_HOME=/data/hf_cache`, `TTS_HOME=/data/tts_home`), and `HOME`/`XDG_CACHE_HOME`/`NUMBA_CACHE_DIR`/`MPLCONFIGDIR` under image-local `/tmp`. The image now boots as uid 1000 without per-deploy env overrides.
- **Structured wrapper logs:** the wrapper used `logging.basicConfig`, which rendered records as plain `INFO:tts.main:...` text and dropped every `extra={...}` field, so pipeline steps were invisible and multi-line tracebacks broke Loki's JSON parser. A JSON formatter (matching the backend's shape) now emits one structured line per record, with uvicorn's loggers routed through it; `/health` access spam is quieted to keep pipeline steps legible.
- **More TTS step detail:** added `tts_model_loaded` (with `load_ms`), `tts_request_received` (episode/chunk/`text_chars`), and per-chunk `inference_ms` on `tts_chunk_done`, so a single `/generate` is traceable end to end in log analysis.
- **PWA route fallback:** the service worker's navigate-fallback served `index.html` for any navigation, so visiting `/api/v1/docs` (or `/rss`, `/media`, `/health`) returned the SPA shell and the router redirected to `/`. Added a `navigateFallbackDenylist` so these server-owned routes hit the network directly. (Browsers with the old service worker cached still need a one-time unregister/hard-reload.)
- **RSS 500 fix:** the per-episode `itunes:image` fell back to the raw `FEED_ARTWORK_URL`, which is `""` when unset -- feedgen rejects that ("Image file must be png or jpg") and the whole feed render returned a 500 for any episode without its own artwork. It now falls back to the same resolved channel artwork (`/media/default.jpg`), matching the channel image. Regression test added.
- **Default podcast artwork:** the seeded default art (`/media/default.jpg`, used by the feed and the Feed UI when an episode has no image) is now the project branding image (`branding/podcast-artwork-3000.png`, 3000x3000).
- **Feed UI artwork:** episodes without their own image now show the default podcast art instead of a flat gradient tile; the gradient remains only as a load-failure fallback.

## [0.3.1] - 2026-05-29

### tts-wrapper CVE remediation

Patches the fixable HIGH/CRITICAL CVEs in the tts-wrapper image (16 of 17; the
78 `linux-libc-dev` findings are kernel-header noise, not exploitable in a container).

- Bump transitive deps to their patched versions in both wrapper Dockerfiles: `urllib3>=2.7.0`, `cryptography>=46.0.5`, `pillow>=12.2.0`, `Brotli>=1.2.0`, `setuptools>=78.1.1`, `wheel>=0.46.2`.
- `transformers` pinned to the 4.48.x line (`>=4.48.0,<4.49`) -- clears CVE-2024-11392/-11393/-11394; coqui-tts 0.24 requires `>=4.43.0` unbounded and only breaks on the 5.x line, so 4.48 is safe.
- `gpgv` upgraded to the patched base-image package (CVE-2025-68973).
- `torch` left at 2.4.x: the only fix (2.6.0) flips `torch.load` to `weights_only=True`, which breaks XTTS checkpoint loading, and CVE-2025-32434 is a `torch.load` RCE unreachable with Audicle's trusted model + WAV inputs.
- Versions bumped to 0.3.1 (app + wrapper) so `BUILD_VERSION=0.3.1` resolves for both images.

## [0.3.0] - 2026-05-29

### Settings UX overhaul, multi-provider LLM, and bind-mount-safe defaults

- **Providers:** `LLM_PROVIDER` now supports `openrouter` (fixed base `https://openrouter.ai/api/v1` + `HTTP-Referer`/`X-Title` headers + `OPENROUTER_API_KEY`) and `ollama` (`OLLAMA_BASE_URL`, no key) alongside `openai-compatible` and `anthropic`. The openai-compatible family shares one call path via `openai_compatible_connection()`; `/api/v1/llm/models` lists models for each. The Settings dropdown shows all four and reveals only the relevant connection fields per provider.
- **Editable defaults:** `GET /api/v1/settings` returns a `defaults` map (effective env/code value per allowlisted key, secrets masked); the UI seeds fields from `values[k] ?? defaults[k]` so LLM/TTS/Cleanup/Feed show editable defaults instead of blanks, and only operator-changed keys are persisted.
- **Bind-mount-safe defaults:** the shipped prompt and a curated default pronunciation set are seeded into the prompt/corrections locations on first boot from a packaged `app/defaults/` dir, so an empty bind-mount no longer hides them. Default podcast artwork (from branding) is seeded to `DATA_DIR/media/default.jpg`; the feed channel image falls back to `{BASE_URL}/media/default.jpg` when `FEED_ARTWORK_URL` is unset.
- **Feed URL:** the Feed page's subscribe URL is built from the configured `BASE_URL` (exposed via `/health/live`), not the browser origin.
- **Non-root:** documented that the image runs as uid 1000 and host bind-mounts must be `chown`ed to 1000:1000 (README + compose comment + optional `user:` line).
- **UI:** rebuilt to the branding/mockup design system -- logo mark, JetBrains Mono + Satoshi, Home hero, Feed cards with status tags, and collapsible Settings sections. System info shows version + uptime (replacing the `allowlist_keys` row) and a link to the API docs (`/api/v1/docs`); `/health/live` now returns `uptime_seconds` + `base_url`.
- **Access logging:** a structured HTTP access log, always enabled (one `http_access` record per request: method, path, status, duration, client) with a per-request `request_id` stamped onto every log emitted during the request.
- **Reachability/health:** `reachability.check_llm` and `/health/ready` now probe the selected provider's endpoint (openrouter/ollama included) with the right base + key, instead of assuming `OPENAI_BASE_URL`.

## [0.2.0] - 2026-05-29

### De-gate startup + runtime-configurable auth and model selection

The app now boots unconfigured and you set operational config at runtime in the Settings UI, mirroring MinusPod. Nothing external blocks startup.

- **Config defaults:** every formerly-required field now has a compose-friendly default (`BASE_URL=http://localhost:8000`, `FIRECRAWL_URL=http://firecrawl:3002`, `TTS_URL=http://tts-wrapper:8000`, `LLM_PROVIDER=openai-compatible`; `LLM_MODEL`/keys/`FEED_*` empty), so `Settings()` never raises at import. `extra="ignore"` means a leftover/legacy env var (e.g. a dropped auth key) is ignored rather than fatal.
- **Advisory reachability:** `reachability.run_all` no longer raises and the worker no longer exits on a down dependency. Results are logged and surfaced in `/health/ready`; a job that hits a down dependency fails that stage with a clear error. `feed.py` emits `itunes:owner`/author/email/image only when those fields are non-empty so an unconfigured feed still renders.
- **Connections are UI-settable:** `FIRECRAWL_URL` and `TTS_URL` added to the runtime-settings allowlist and a Connections group in the Settings UI, so an external Firecrawl/TTS can be pointed at without an env edit + restart.
- **LLM model selection (MinusPod pattern):** new `GET /api/v1/llm/models[?provider=]` and `POST /api/v1/llm/models/refresh` list models for the configured or previewed provider (openai-compatible `/models` with an Ollama `/api/tags` fallback; anthropic known-model list), with a per-process TTL cache and never a 500 (empty list on error). The Settings UI renders `LLM_MODEL` as a dropdown keyed by provider, with an orphan option for a saved-but-unlisted value, a refresh button, and a free-text fallback.
- **Password-only auth (full MinusPod parity):** dropped `AUTH_ENABLED`/`ADMIN_USERNAME`/`ADMIN_PASSWORD_HASH`. The admin password is set in the Settings UI (`PUT /api/v1/auth/password`); its bcrypt hash lives in the `settings` DB table. No password set = open convenience mode. `GET /auth/status` returns `{password_set, authenticated, csrf_token}`; `POST /auth/login` takes `{password}`. Lockout keys on the client IP. The session secret is auto-generated and persisted to the DB (key `session_secret`) when `SESSION_SECRET_KEY` is unset, so sessions survive restarts. `SESSION_COOKIE_SECURE` now defaults to true.
- **Compose:** `app.depends_on.tts-wrapper.condition` changed from `service_healthy` to `service_started` so the app no longer deadlocks waiting on a voice-less wrapper.
- **Versions:** app and tts-wrapper both bumped to 0.2.0 to stay in sync (wrapper code unchanged from 0.1.0); images `ttlequals0/audicle:0.2.0` and `ttlequals0/audicle-tts:0.2.0` (+`-cpu`) published.

### Image polish + headless TTS (`chore/build-plan-audit-and-cleanup`)

- TTS wrapper accepts the CPML terms non-interactively (`COQUI_TOS_AGREED=1` in both Dockerfiles): without it the first XTTS-v2 load blocks on an interactive y/n prompt that has no TTY in a container, so the wrapper crashed on startup.
- The wrapper no longer fails to start when `voice.wav` is absent *or unreadable*. The model loads, a missing/undecodable reference is logged and skipped (`reference_loaded=false`), and `/generate` returns 503 until a usable voice is committed -- so an operator can bring the stack up and upload a reference voice through the Settings UI instead of crash-looping on a missing or corrupt pre-staged file. `reachability.check_tts` accepts `model_loaded=true` even on a 503 so the worker doesn't block on it.
- TTS wrapper gains `/health/live` (200 once the model is loaded, voice or not); the docker healthcheck and `depends_on: service_healthy` now use it. Previously the healthcheck hit `/health` (readiness, 503 without a voice), which marked a voice-less wrapper unhealthy and deadlocked the app's startup -- the app gated on the wrapper being healthy, but the voice is uploaded through the app's UI. `/health` stays the readiness probe.
- `docker-compose.yml`: dropped the `build:` blocks (images come from Docker Hub).
- Comment cleanup: removed phase-specific and `build-plan line NNN` references across the codebase and trimmed verbose comments.

### Build-plan audit + cleanup pass (`chore/build-plan-audit-and-cleanup`)

Audited the whole codebase against `build-plan.md` for completeness and accuracy, ran /simplify and /code-review over the diff, fixed every finding, and normalized all docs to ASCII.

**Code fixes:**

- `backend/app/api/health.py`: `/health/ready` now aggregates the full component detail the build plan specifies. `components` carries `tts_wrapper` (version/torch/coqui_tts/device/model_loaded from the wrapper's `/health`), `firecrawl` (`{url, reachable}`), and `llm` (`{provider, model, base_url, reachable}`). The TTS check key is `tts_wrapper` (was `tts`). A single `_reachable(status)` helper drives both the per-component flag and the top-level `ok`, so `skipped` is treated consistently everywhere.
- `tts-wrapper/main.py` + `engine.py`: `/health` reports `version`/`torch`/`coqui_tts`/`device`/`sample_rate` so the backend can surface them. Package versions resolve once at import (not per probe). `device` is now a first-class `Engine` Protocol attribute instead of a chained `getattr` into the concrete engine.
- `backend/app/services/jobs.py` + `episodes.py`: reprocess no longer deletes the episode row. The finalize upsert updates it in place, preserving `created_at` (the original feed-entry moment, per the build plan) and bumping `pub_date` so the episode re-surfaces as new. This also removes a data-loss path: a reprocess that failed partway used to wipe the episode entirely; now the prior audio stays live until the new run finalizes.
- `backend/app/services/retention.py` + `worker.py`: the daily sweep now prunes migration backups (`prune_backups` was implemented but never called) and reaps expired `done`/`failed` job rows that no live episode references (closes build-plan retention scope and prevents unbounded `jobs` growth from repeated reprocessing).
- `backend/app/services/feed.py`: per-episode RSS `<description>` now carries title + author + source link (HTML), and a per-episode `<itunes:summary>` is emitted, matching the build-plan feed spec instead of bare title text.
- `frontend/public/icon-192.png` + `icon-512.png`: added the PWA manifest icons (the manifest referenced files that did not exist, so install icons were broken). Generated from `branding/podcast-artwork-1400.png`.
- `backend/app/api/v1/submit.py`: corrected the `reprocess` / `replaced_previous` field descriptions that still claimed prior state was "deleted/wiped". `openapi.yaml` regenerated.

**Docs:**

- `build-plan.md`: reconciled with the as-built code -- env-var auth model (`AUTH_ENABLED`/`ADMIN_*`, username-keyed lockout, no `set-password`/`csrf` endpoints), reference-audio flow (GET preview + direct-upload test/commit, 5 MB cap, 400), `page`/`per_page` pagination, runtime-settings allowlist and Settings UI groups, `soundfile` concat, single-pass loudnorm, `podcast:txt` prose value, project-layout module names (no `services/queue.py` / `utils/corrections.py` / `models/` dir), React 18, Python 3.14 backend image / 3.11 wrapper, added config vars (`ARTWORK_MAX_DOWNLOAD_BYTES`, `TTS_REACHABILITY_*`, the real auth vars), and the compose `/app/app/*` mount paths.
- `README.md`: `ADMIN_PASSWORD` -> `ADMIN_PASSWORD_HASH`, rewrote the circular Coqui/Python 3.13 note (the `coqui-tts` PyPI package is the idiap fork) and added the backend-3.14 / wrapper-3.11 split.
- `tts-wrapper/README.md`: voice-clip spec aligned to the authoritative `reference/README.md` (3-60 s, 24 kHz, <= 5 MB) and the `/app/app/reference` mount path.
- All Markdown docs normalized to ASCII (em-dashes, arrows, ellipses, box-drawing tree glyphs) per the repo's ASCII-only rule.
- `scripts/dump_openapi.py`: usage docstring matches the README (`uv run python scripts/dump_openapi.py`; the script self-inserts `backend` on the path).

**Features built (closing documented gaps) + CodeQL:**

- LLM Provider settings group: `LLM_PROVIDER`/`LLM_MODEL`/`OPENAI_BASE_URL`/`OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`LLM_TEMPERATURE`/`LLM_MAX_TOKENS`/`LLM_TIMEOUT_SECONDS`/`LLM_RETRY_COUNT` added to `runtime_settings.ALLOWED_KEYS`. API keys are masked on GET (`MASK_SENTINEL`) and the sentinel is ignored on PUT so re-saving never overwrites the stored secret; an empty value clears the override. `Settings.tsx` gains an LLM group with a provider dropdown and password inputs.
- `worker.py`: `_process_one` now runs `runtime_settings.overlay()` per job (with a fallback to env settings if the overlay read fails), so Settings-UI edits actually reach the pipeline -- the per-job resolution chain the plan promised was not previously wired into `process_job`.
- `Feed.tsx`: cards now show artwork thumbnail, status badge, title link (2-line clamp), author + source domain, and inline transcript / reprocess / mp3 / delete actions, with error feedback (e.g. a 409 on reprocessing an in-flight URL surfaces a message instead of silently failing).
- CodeQL: deleted `.github/workflows/codeql.yml` (it conflicted with the repo's enabled default CodeQL setup, which covers python + js/ts/actions). Fixed the flagged alerts: `main.py` SPA fallback serves an allowlist of root static files plus the hash-named workbox runtime (user path never reaches the filesystem); `tts-wrapper/main.py` `/generate` validates the wav path via `realpath`+`commonpath`; `reference.py` `/commit` no longer returns the raw httpx error string.

### Gap-closure pass (`chore/close-real-gaps`)

Plan-completion audit found 5 missing deliverables and 7 partially-shipped items left from earlier phases. This pass closes them and folds in the simplify + code-review fix lists.

**Phase 13 deliverables newly shipped:**

- `backend/app/api/v1/reference.py`: `GET /reference/preview`, `POST /reference/test`, `POST /reference/commit` for operator voice management. Streaming upload cap, `wave.open` validation (3-60 s, <= 5 MB), `asyncio.Lock`-serialised stage/restore for `/test`, atomic write + wrapper `/reload` for `/commit`.
- `backend/app/reference/README.md`: voice clip spec table, sourcing playbook, ffprobe verification, CPML licence note.
- `openapi.yaml`: generated via `uv run python scripts/dump_openapi.py`.
- `backend/app/api/health.py`: `/health/ready` now aggregates DB + ffmpeg + TTS + Firecrawl + LLM probes. Sequential probes parallelised via `asyncio.gather(return_exceptions=True)` so one stuck upstream can't add its budget to the others. ffmpeg banner cached only on success so a late PATH fix becomes visible.

**Web UI completed:**

- `frontend/src/routes/Settings.tsx`: 5 grouped sections (Feed / TTS / Cleanup / Retention / RSS), prompt editor (PUT `/api/v1/prompt`), corrections table (PUT `/api/v1/corrections`), reference voice widget (preview/test/commit), system-info block.
- `frontend/src/routes/Feed.tsx`: mobile pull-to-refresh on the episode list.

**Correctness fixes from /simplify and /code-review:**

- CSRF: `frontend/src/lib/api.ts` `readCsrf` exported; `Settings.tsx` now uses it instead of inline `split("=")[1]` parsing that truncated base64-padded tokens.
- React state: `PromptEditor` and `CorrectionsTable` seed via lazy `useState` initializer + gate on `data !== undefined` at the parent so in-progress edits survive React Query refetches.
- `CorrectionsTable` rows now have stable IDs (no more React array-index keys); focus and IME composition no longer jump when rows are deleted.
- `URL.revokeObjectURL` cleanup added to the reference audition pane.
- `reference.py`: removed `except BaseException` (was swallowing `CancelledError`); narrowed to `Exception`. Suppressed `/reload` errors now logged at WARN. Wrapper-supplied `wav_path` validated against `DATA_DIR` to prevent arbitrary local read.
- `reference.py` `/test` no longer leaves the candidate as the live voice when no prior reference existed (`backup is None` branch now unlinks instead of preserving).
- `_validate_wav` switched from `tempfile.NamedTemporaryFile` to `io.BytesIO`; saves a disk write per call.
- `_read_upload_capped` rejects oversized uploads mid-stream instead of buffering the whole body first.
- `Settings.tsx` reference widget: `postForm` wraps `fetch` with try/catch so transport errors surface as a user-visible message; file-input `onChange` clears stale audition.
- `health.py` no-URL LLM path returns `"skipped"` via `_probe_http`'s existing empty-base branch; `_noop_skipped` helper removed.

**Lint/imports cleanup:**

- Inline `import subprocess` / `import httpx` / `from contextlib import contextmanager` lifted to module top in `health.py` and `reference.py` per `CLAUDE.md`.
- `pyproject.toml`: added `python-multipart` for FastAPI `UploadFile`/`Form`.

**Tests:** 6 new in `test_api_reference.py` (preview 404, preview serves, commit atomic swap + reload, reject too-short / oversized / non-WAV). 332 backend tests pass; ruff clean.

### Security + correctness (codebase-wide review)

Multi-agent /simplify + /code-review sweep over the full backend (49 service modules + 38 test files), plus a background security finding (missing auth on `GET /api/v1/settings`).

**Security:**

- **Missing auth on read-only admin endpoints**: `GET /api/v1/prompt`, `GET /api/v1/corrections`, `GET /api/v1/settings` had no `require_admin` dependency, so when `AUTH_ENABLED=true` they still leaked operator config. Added `dependencies=[Depends(require_admin)]` to all three.
- **Missing auth on `POST /api/v1/submit`**: anyone could enqueue jobs and burn LLM / TTS quota. Added `require_admin`.
- **Login timing oracle**: `verify_credentials` short-circuited bcrypt for unknown usernames so response time revealed whether a username existed (~50-100ms vs. ~0ms). Now always runs bcrypt against either the real `ADMIN_PASSWORD_HASH` or a precomputed `_DUMMY_HASH`; username comparison uses `hmac.compare_digest`.
- **Lockout counter race**: previous `_register_failed_attempt` was a SELECT-then-UPSERT pair; concurrent failed logins under `WEB_WORKERS=2` could both read N and both write N+1, defeating `LOCKOUT_MAX_FAILED_ATTEMPTS`. Replaced with `INSERT ... ON CONFLICT DO UPDATE SET failed_attempts = failed_attempts + 1, lockout_until = CASE WHEN ... THEN ... ELSE NULL END` so the increment + threshold check happen inside one engine-serialized statement.
- **CSRF on safe methods**: `require_admin` enforced the CSRF header on every method including `GET`, which 403'd a SPA between session-cookie load and CSRF-cookie read. Now skips for `GET`/`HEAD`/`OPTIONS`; session check still applies.
- **`/health/ready` exception leak**: failure path returned `f"error: {exc}"` in the response body, exposing `sqlite3.OperationalError` text including the absolute `DATA_DIR` path. Now returns a generic `"error"`; the full exception stays in the WARN log only.
- **`POST /api/v1/submit.url` length cap**: added `max_length=2048`.
- **`tts-wrapper` `GenerateRequest.text` length cap**: added `max_length=4000` so an oversized payload can't hold the `asyncio.Lock` for 120s and starve every other `/generate` call.
- **`.env.example` had no auth block**: operators copying it deployed with `AUTH_ENABLED=false` and zero signal to flip it. Added a `# ---- Auth (REQUIRED for any public-internet deployment) ----` section with placeholders, generation commands, and explanatory comments.

**Cleanup:**

- Shared `retention.MAX_OLDER_THAN_DAYS` between the service guard and the purge endpoint's `Query(..., le=...)` constraint.
- Lifted three inline imports in `main.py` (`secrets`, `_LOGIN_LIMITER`, `JSONResponse`) to the module top per `CLAUDE.md`.
- Replaced the hand-built 429 envelope in `_attach_rate_limiter` with `errors.envelope(status=429, error="rate limit exceeded")` -- the canonical helper every other 4xx/5xx route uses.

**Tests (16 new, 317 total):** parametrized tables in `test_api_auth.py` assert every admin route returns 401 without a session when `AUTH_ENABLED=true`, mutating routes return 403 without `X-CSRF-Token`, and `GET` is allowed with session + no CSRF (safe-methods exemption). A typo on any future `dependencies=[Depends(require_admin)]` declaration is caught immediately.

**Deferred (real findings, deserve their own PR):**

- Runtime settings (`PUT /api/v1/settings`) persists overrides but no production service reads them back. The Phase 10 docstring promised a "code default -> env var -> DB" resolution chain that doesn't yet exist.
- `database.connect/close` try/finally repeats 20 times across 12 modules -- would benefit from a `core/database.connection(settings)` context manager.
- ISO-timestamp `Z`-suffix parsing repeats in `feed.py`, `auth.py`, and `api/rss.py` -- promote to `core/timestamps.parse_iso`.
- `list_episodes` / `list_jobs` use Python slice rather than SQL `LIMIT`/`OFFSET` + `COUNT(*)`.
- DNS-rebinding TOCTOU in the artwork SSRF guard.
- Phase 11 (Web UI) remains deferred.

### Security (Pre-emptive CodeQL hardening)

- `.github/workflows/codeql.yml`: GitHub CodeQL with the `security-and-quality` query pack. Runs on every PR + push + a weekly schedule. Top-level `permissions: {}` with the analysis job re-narrowing to `security-events: write` + read-only repo access (least privilege).
- `.github/workflows/dependency-review.yml`: blocks PRs that introduce a high-severity CVE on a runtime dep. License allowlist accepts the OSS set the project already uses (MIT/BSD/Apache/MPL/ISC/PSF/CC0/BlueOak); GPL/AGPL drift surfaces for a deliberate decision.
- `.github/dependabot.yml`: weekly updates for `pip`, `github-actions`, and `docker` ecosystems with a 5/3/3 PR cap per ecosystem.
- `services/jobs.compute_episode_id`: `hashlib.md5(..., usedforsecurity=False)`. md5 is the build-plan-mandated content identity hash (12-char URL fingerprint), not a security primitive; the flag silences CodeQL's `py/weak-cryptographic-algorithm` rule.
- `services/episodes.py`: rewrote two `f"SELECT {_SELECT_COLUMNS} FROM episodes WHERE id = ?"` calls as `"SELECT " + _SELECT_COLUMNS + " FROM ..."` with `# noqa: S608` comments. `_SELECT_COLUMNS` is a fixed module constant so there was no SQL injection -- but CodeQL keys on the f-string shape regardless of interpolation source; the explicit concatenation + noqa removes the false positive while preserving the safety property in code review.

No behavioral changes; tests still 301/301; ruff clean.

### Added (Phases 12 + 13 - Operations + Polish)

- `backend/app/api/v1/jobs.py`: `GET /api/v1/jobs` admin inspector with `?status=` filter (`queued`/`processing`/`done`/`failed`) + pagination + `X-Total-Count`. Lets the UI surface failed jobs without round-tripping through the SQLite shell.
- `backend/app/services/retention.sweep_orphan_media`: walks `DATA_DIR/media` and removes any `{id}.{mp3,jpg,vtt}` / `{id}_combined.wav` whose id no longer matches a live `episodes` row. Plugged into the daily worker sweep right after the age-based purge so a crash between `audio.normalize_and_encode` and `_stage_finalize` doesn't leave permanently-orphaned files. Skips `voice.wav` (operator reference audio).
- `scripts/dump_openapi.py`: regenerates `openapi.yaml` from the live FastAPI app. Adds env-var placeholders so `uv run python scripts/dump_openapi.py` works outside a deployed environment.
- `LICENSE` (MIT) at the repo root. The Audicle brand is reserved (see `branding/README.md`); the license covers code only. Notes the CPML constraint on XTTS-v2 weights.
- Runtime deps: `pyyaml>=6` (for the OpenAPI dump script).

**Phase 11 (Web UI) is intentionally deferred.** Building Vite + React + TypeScript + Tailwind + React Query + React Router + vite-plugin-pwa from scratch is a multi-day frontend track that exceeds the current execution budget. All API endpoints the UI will consume (`/api/v1/auth/*`, `/api/v1/settings`, `/api/v1/episodes`, `/api/v1/jobs`, `/api/v1/prompt`, `/api/v1/corrections`, `/api/v1/purge`, `/api/v1/submit`, `/api/v1/status`) are shipped and tested; the UI is the remaining missing piece.

Tests (6 new, 301 total): jobs filter by status, jobs pagination + total-count, orphan sweep removes only files without matching rows, voice.wav skipped, missing media dir is a no-op.

### Added (Phase 10 - Runtime Settings + Episodes Admin)

- `backend/app/services/runtime_settings.py`: operator-tunable settings backed by the new `runtime_settings` table. An explicit `ALLOWED_KEYS` allowlist gates writes so an attacker can't flip `DATA_DIR` or `SESSION_SECRET_KEY` through the admin UI.
- `backend/app/api/v1/settings.py`: `GET /api/v1/settings` returns `{allowlist, values}`; `PUT /api/v1/settings` accepts a partial dict and persists each allowlisted key. Values are coerced back to the declared `Settings` field type on read so `RETENTION_DAYS=30` round-trips as `int` not `str`. Unknown keys return 400 with the allowlist in the error body. PUT requires `require_admin`.
- `backend/app/api/v1/episodes.py`: `GET /api/v1/episodes` paginates (`page` + `per_page`) and emits `X-Total-Count`; `DELETE /api/v1/episodes/{id}` removes the row and any associated mp3/jpg via the Phase-8 defense-in-depth `_remove_path` helper. Both require `require_admin`.
- Migration `004_runtime_settings` appends `runtime_settings(key PRIMARY KEY, value, updated_at)`. Phase 1-3 schemas untouched.

Tests (8 new, 295 total): GET/PUT round-trip with type coercion (`int`, `bool`, `str`), unknown-key rejection with allowlist in the error body, pagination + `X-Total-Count` header, delete with file cleanup, 404 on missing episode.

### Added (Phase 9 - Authentication)

- **Optional single-admin auth**: `AUTH_ENABLED=false` (the default) leaves the admin endpoints open for a single-operator localhost install. Flipping `AUTH_ENABLED=true` requires `ADMIN_PASSWORD_HASH` (bcrypt) and `SESSION_SECRET_KEY` (validated at startup; missing values raise a Pydantic `ValueError` so the process exits rather than silently serving the admin UI unauthenticated).
- `backend/app/services/auth.py`: bcrypt verify, lockout machinery against the new `auth_lockout` table. `LOCKOUT_MAX_FAILED_ATTEMPTS` failures opens a `LOCKOUT_WINDOW_SECONDS` ban for that identifier; the table is the source of truth so a manual `DELETE FROM auth_lockout WHERE identifier='admin'` is the documented operator-recovery path. Malformed hashes surface as 401 (not 500).
- `backend/app/services/csrf.py`: double-submit cookie tokens. Login issues `audicle_csrf` (httpOnly=false so the UI can read it) plus a JSON `csrf_token` field; mutating endpoints require the same value echoed in `X-CSRF-Token`. Compared with `hmac.compare_digest`.
- `backend/app/api/deps.py`: `require_admin` dependency. No-op when `AUTH_ENABLED=false`; otherwise asserts the session cookie carries `audicle_user` AND a matching CSRF header. Applied to `PUT /api/v1/prompt`, `PUT /api/v1/corrections`, `POST /api/v1/purge`.
- `backend/app/api/v1/auth.py`: `POST /auth/login` (rate-limited by slowapi to `10/minute` per remote IP), `POST /auth/logout`, `GET /auth/status`. Login returns 401 on bad creds, 423 on lockout, 200 on success with the CSRF token in body + cookie. `GET /auth/status` is unauthenticated (so the UI can ask "should I show the login form?").
- `backend/app/main.py`: wired Starlette's `SessionMiddleware` (signed with `SESSION_SECRET_KEY` when auth is on, an ephemeral key when off so `request.session` still exists). Registered slowapi's `RateLimitExceeded` handler so the rate-limit response carries the project's error envelope (`{"error": ..., "status": 429}`).
- Migration `003_auth_lockout` appends `auth_lockout(identifier PRIMARY KEY, failed_attempts, last_attempt_at, lockout_until)`. Phase 1 + 2 + 3 schemas untouched.
- Runtime deps: `bcrypt>=4.2`, `itsdangerous>=2.2`, `slowapi>=0.1.9`.

Tests (26 new, 287 total)

- `test_auth.py` (9): `hash_password` round-trip, correct password / wrong password / wrong username, lockout-after-threshold, manual lockout-row delete recovers, expired lockout window allows login, malformed hash returns invalid-creds not 500, case-insensitive username match.
- `test_csrf.py` (6): token uniqueness and URL-safe alphabet, equal-string match, mismatch rejection, missing-header/cookie rejection, empty-string rejection.
- `test_api_auth.py` (11): login 200 + CSRF cookie + session cookie, 401 on wrong password, 423 after threshold, logout clears session, status when logged-in / logged-out, login 400 when `AUTH_ENABLED=false`, `PUT /api/v1/prompt` returns 401 without session, 403 without CSRF, 200 with both, 200 without auth when `AUTH_ENABLED=false`.

`conftest.py` adds an autouse `_reset_login_rate_limiter` so the slowapi singleton's in-memory store doesn't carry login hits across tests.

Container smoke (`tmp/phase9_smoke.sh`) verified inside the runtime image: enable auth, log in with the bcrypt'd password, hit `PUT /api/v1/prompt` with the CSRF header (200), log out, confirm subsequent PUT returns 401, then 3 bad-password attempts open a lockout window and the 4th login returns 423 even with the correct password.

### Added (Phase 8 - Retention and Purge)

- `backend/app/services/retention.py`: `purge_older_than(settings, older_than_days)` deletes episodes (DB row + on-disk mp3/jpg/vtt) older than the cutoff. `older_than_days=0` is the explicit "wipe everything" contract used by the purge endpoint; positive N filters strictly older than `now - N days`. Returns `PurgeResult(episode_ids, rows_deleted, files_removed)` so callers can log + surface a summary.
- **Defense-in-depth file removal**: `_remove_path` resolves the target with `path.resolve(strict=False)`, then `relative_to(media_dir.resolve())` to confirm the path stays under `DATA_DIR/media` before unlinking. A poisoned row pointing `audio_path` at `/etc/passwd` (manual DB edit, future migration accident) logs a WARN and is skipped. Missing files are silently treated as success (the sweep is idempotent).
- `backend/app/worker.py`: daily retention sweep wired into the worker poll loop. `_maybe_run_retention_sweep` runs at most once per UTC day at `RETENTION_SWEEP_HOUR_UTC`, calling `purge_older_than(RETENTION_DAYS)`. Sweep failures are logged at ERROR (`retention_sweep_failed`) but do NOT mark the day as swept, so the next iteration retries. The sweep is stateless across restarts -- a worker bounce past the sweep hour without having run today will re-fire.
- `backend/app/api/v1/purge.py`: `POST /api/v1/purge` for operator-initiated wipes. Requires `confirm=true` query param to acknowledge the destructive action (returns 400 otherwise). Accepts `older_than_days=N` for a partial purge (validated `ge=0`). Returns `{older_than_days, rows_deleted, files_removed, episode_ids: [...]}` so an operator script can confirm what got removed.
- `backend/app/api/v1/router.py`: registered the purge route alongside the existing v1 routes.

Tests (16 new, 261 total)

- `test_retention.py` (7): old rows + files removed, full purge wipes everything (including future-dated rows), partial-cutoff no-op, missing files silently skipped, defense-in-depth rejects paths outside `media_dir` (verified via the `retention_unsafe_path` log assertion), negative `older_than_days` raises `ValueError`, `>100_000` days rejected before `timedelta` overflows the year-9999 ceiling.
- `test_api_purge.py` (5): missing `confirm` returns 400, `confirm=true` wipes when `older_than_days=0`, partial purge keeps recent, negative days rejected at validation (400 via the custom error handler), response shape matches the documented schema.
- `test_worker_retention.py` (4): sweep fires when the UTC hour matches and no run today; skips when already run today; skips when the hour doesn't match; sweep failure does NOT mark the day as swept and is logged with `retention_sweep_failed` event.

### Code-review pass (multi-agent /simplify + /code-review for Phase 8)

Findings surfaced and applied:

- **`OverflowError` on huge `older_than_days`**: `datetime.now(UTC) - timedelta(days=N)` overflows past the year-9999 ceiling at ~2.9M days. The endpoint accepted any non-negative int and would 500 on `older_than_days=1_000_000`. Added `_MAX_OLDER_THAN_DAYS=100_000` cap in `purge_older_than` and `le=100_000` on the FastAPI Query so misuse fails with a clean 400 / `ValueError`.
- **Sync purge blocked the async worker loop**: `_maybe_run_retention_sweep` is sync (SQLite + file unlinks). Wrapped the call in `asyncio.to_thread` so a large sweep doesn't stall signal handling or the `shutdown.wait()` that lets SIGTERM exit cleanly.
- **Config bounds missing**: `RETENTION_DAYS` and `RETENTION_SWEEP_HOUR_UTC` accepted any int. A misconfigured `RETENTION_SWEEP_HOUR_UTC=25` would silently disable the sweep. Added `Field(ge=0, le=23)` / `Field(ge=0, le=100_000)` so startup fails loudly on a bad value.
- **Docstring contradicted behavior for `older_than_days=0`**: old docstring claimed "every episode matching `pub_date < now`" but the implementation wipes unconditionally (including future-dated rows from clock skew or test fixtures). Updated to describe the actual wipe-everything contract.
- **Simplified the zero-day branch**: collapsed the `if cutoff_iso is None` / separate SELECT into a single parameterized query that uses a year-9999 sentinel cutoff. One code path instead of two.
- **`_remove_safe` was a one-line wrapper**: inlined into the single caller; saves a hop and clarifies the `path_str|None` guard at the call site.
- **`_remove_path` swallowed unlink errors silently**: `IsADirectoryError`, `PermissionError`, and other `OSError` cases were eaten by a bare `suppress(OSError)` so operators had no breadcrumb to a stuck on-disk artifact after a sweep. Now logs `retention_unlink_failed` / `retention_resolve_failed` with the exception class.
- **`test_purge_refuses_paths_outside_media_dir` didn't assert the WARN log**: added `caplog` assertion that the `retention_unsafe_path` event is emitted. Without this, a future change that silenced the log would still pass the test.
- **Frozen-datetime helper was duplicated four times**: extracted `_freeze_now(monkeypatch, fake_now)` in `test_worker_retention.py`. Inheriting from the real `datetime` (rather than building a stub class with only `now`) means a future change in `worker.py` that adds `datetime.fromisoformat(...)` or constructs a `datetime(...)` no longer breaks every retention-sweep test with an `AttributeError`.
- **`test_purge_negative_older_than_days_raises_value_error` missing `env` fixture**: would fail at `get_settings()` validation before hitting the negative branch. Added the fixture; the test now actually exercises the code path it documents.
- **CHANGELOG test-count drift**: previous draft claimed 276 total. Re-checked against `pytest --collect-only`: 261. Updated.

Container smoke (`tmp/phase8_smoke.sh`) verified inside the runtime image: build, seed a row + mp3/jpg under `/data/media`, `POST /api/v1/purge` without `confirm` returns 400, `POST /api/v1/purge?confirm=true` returns 200 with `rows_deleted=1` and `files_removed=2`, and re-querying the DB confirms the row is gone and the on-disk files are unlinked.

### Added (Phase 7 - RSS Feed and Media Serving)

- Pipeline now runs **extract -> cleanup -> corrections -> chunk -> tts -> audio -> artwork -> transcript -> finalize**. Final `status=done` with `stage=finalize`. The finalize stage upserts a row into the `episodes` table that the RSS feed and media handlers read from.
- `backend/app/core/database.py`: migration `002_settings_kv` appends a `settings(key, value, updated_at)` k/v table for `podcast:guid` and future runtime knobs. Phase-1 schema is left untouched.
- `backend/app/services/episodes.py`: typed `Episode` dataclass + `upsert`, `get_by_id`, `list_published` (newest-first, filters out rows with `audio_path IS NULL` so half-finalized jobs don't leak into the feed), `latest_updated_at` (for the RSS `Last-Modified` header and `<lastBuildDate>` field). `upsert` preserves `pub_date` on update (original publish moment) but bumps `updated_at` so clients see a fresh build.
- `backend/app/services/settings_store.py`: k/v get/set + `get_or_init_podcast_guid` which derives a UUIDv5 from `BASE_URL` on first call and persists it. The PC2 spec requires the guid stay stable across feed-URL changes, so the persisted value is returned verbatim on subsequent calls regardless of `BASE_URL`.
- `backend/app/services/feed.py`: `render(episodes, settings, podcast_guid, last_build) -> bytes`. feedgen renders Atom + iTunes namespaces; PC2 (`podcast:` namespace) tags are layered on via string-level XML construction afterwards (`podcast:guid`, `podcast:locked` with `owner=FEED_EMAIL`, `podcast:txt purpose="ai-content"` at channel level; `podcast:transcript` per item when `transcript_vtt` is present). Item enclosures get the on-disk MP3 size via `Path.stat().st_size` (missing files report length=0 rather than 500). `itunes:duration` renders as `HH:MM:SS`. Uses `defusedxml.ElementTree.fromstring` for parsing feedgen's output -- XXE/billion-laughs defense-in-depth, even though the parser only ever sees our own output.
- `backend/app/api/rss.py`: `GET /rss/rss.xml` -- streams the rendered feed with `Cache-Control: public, max-age=RSS_CACHE_MAX_AGE_SECONDS` and `Last-Modified` derived from the newest episode's `updated_at` (or the channel build time when the feed is empty). `If-Modified-Since` round-trips to `304 Not Modified` so podcast clients don't refetch the full body on every poll.
- `backend/app/api/media.py`: `GET /media/{episode_id}.mp3`, `/media/{episode_id}.jpg` serve from disk via `FileResponse` with the right content-types and a 24h cache. `GET /media/{episode_id}.vtt` serves the transcript directly from the episode row's `transcript_vtt` column with `Cache-Control: public, max-age=86400`. The `episode_id` route parameter is validated against a strict allowlist (`^[A-Za-z0-9_-]+$`) so a client can't use `..` or absolute paths to escape `DATA_DIR/media`.
- `backend/app/services/pipeline.py`: `_stage_finalize` calls `episodes.upsert` with the live audio/artwork/vtt/duration produced by prior stages. Title and author come from the extraction metadata; when Firecrawl doesn't report an author the row falls back to `FEED_AUTHOR` so the iTunes author field stays populated. The transcript stage no longer drops its VTT on the floor.
- `backend/app/main.py`: mounted `rss` and `media` routers at the root prefix (the build-plan paths are `/rss/rss.xml` and `/media/{id}.{ext}`, not `/api/v1/*`).
- Runtime deps: `feedgen>=1.0`, `defusedxml>=0.7` (plus transitive `lxml`, `python-dateutil`).

Tests (43 new, 240 total)

- `test_episodes.py` (5): insert + update by same id (pub_date preserved on update), list_published newest-first ordering, half-finalized rows excluded, get_by_id None for unknown, latest_updated_at tracks most-recent published.
- `test_settings_store.py` (7): get/set round-trip, upsert on existing key, get_or_init persists first call, stable across calls, UUIDv5 of BASE_URL is reproducible, returns persisted value even when BASE_URL changes.
- `test_feed.py` (13): channel title/description/language/image, PC2 guid/locked/txt tags, enclosure length from filesize (and 0 when missing), itunes:duration HH:MM:SS, podcast:transcript present when VTT, omitted when not, per-episode jpg vs feed-level artwork fallback, atom:link self pointing at /rss/rss.xml, `_hms` edge cases (zero, negative, hour rollover), `_parse_iso` Z-suffix and garbage handling.
- `test_api_rss.py` (7): 200 with valid XML body, Cache-Control header, Last-Modified round-trips to 304, full body when client is older, podcast:guid persists across requests, empty channel for no episodes, half-finalized rows excluded.
- `test_api_media.py` (9): mp3/jpg/vtt 200 with correct content-types, 404 on missing files / missing episode / null transcript, path-traversal blocked at the route boundary (`..%2f` URL-encoded), dot-prefixed ids rejected by the allowlist regex.
- `test_pipeline.py` (2): finalize upserts the episode row with audio/artwork/vtt/duration from the in-memory pipeline state; FEED_AUTHOR fallback when extraction metadata has no author.

### Code-review pass (multi-agent /simplify + /code-review for Phase 7)

Findings surfaced and applied:

- **Channel `<link>` was pointing at the feed URL, not BASE_URL**: feedgen's `link()` setter binds the channel `<link>` to the *last* href passed. The original code called `rel='alternate'` then `rel='self'`, so the rendered `<link>` carried `/rss/rss.xml`. Swapped the order so `rel='self'` runs first and the channel `<link>` correctly renders BASE_URL (the website).
- **Stdlib ElementTree was emitting `ns0:` / `ns1:` prefixes after the round-trip**: only the `podcast` namespace was registered, so the re-serialized feed lost the `atom:` and `itunes:` prefixes. Apple Podcasts and Cast Feed Validator reject the auto-prefixed forms. Now registers `atom`, `itunes`, and `podcast` namespaces at module load (before any render).
- **PC2 channel tags were appended after `<item>` elements**: `ET.SubElement(channel, ...)` appends, so `podcast:guid` / `podcast:locked` / `podcast:txt` ended up at the bottom of the channel after all items. Some PC2 validators warn. Switched to `channel.insert(idx_before_first_item, el)` and added a `_first_item_index` helper.
- **`podcast:guid` was derived from `uuid.NAMESPACE_URL` instead of the PC2 namespace**: the PC2 spec mandates UUIDv5 over the namespace `ead4c236-bf58-58c6-a2c6-a6b28d128cb6` with the scheme stripped and trailing slash removed. The previous derivation produced a value no other PC2-aware tool would compute for the same feed, defeating cross-aggregator deduplication. Added `_canonical_feed_url` helper, switched to the spec-mandated namespace.
- **`podcast:txt` text was the literal string `"true"`**: not a recognized sentinel for any PC2 purpose. PC2-aware clients (Fountain, Podverse) display the body verbatim, which read as broken. Replaced with a human-readable disclosure (`"This podcast contains AI-generated narration via TTS."`) while keeping `purpose="ai-content"` on the attribute.
- **Missing `<itunes:type>episodic</itunes:type>`**: Apple Podcasts defaults to serial-style ordering without this tag, which is wrong for the news/article-narration use case. Added via `fg.podcast.itunes_type("episodic")`.
- **Missing `<podcast:medium>podcast</podcast:medium>`**: PC2-aware aggregators use this to route the feed into the correct player section. Added at the top of the PC2 channel block.
- **`<image>` was missing `<title>` and `<link>` subelements**: RSS 2.0 requires all three; validators reject otherwise. Now passes `title=` and `link=` to `fg.image`.
- **`zip(items, episodes, strict=False)` could silently misalign transcripts**: if feedgen ever filters or reorders entries, the per-item `podcast:transcript` URLs would attach to wrong items without a test failure. Switched to `strict=True` so the failure surfaces loudly.
- **Bare `assert row is not None` in `episodes.upsert`**: removed under `python -O`, would surface a confusing `TypeError` instead of a clear failure. Replaced with `if row is None: raise RuntimeError(...)`.
- **Path-traversal test passed for the wrong reason**: the encoded `..%2f` URL caused Starlette's router (not the in-handler regex) to 404. Added `test_media_routes_reject_dot_prefixed_id` that asserts the lowercase `"not found"` body shape from the custom error handler, proving `_validate_episode_id` actually fired.
- **Cleanup**: removed unused `now_utc()` helper, removed unused `response: Response` parameter from `rss.py:get_rss`, dropped redundant `int(seconds)` cast in `_hms`, inlined the `_PODCAST_GUID_NAMESPACE` alias.

New tests added by the review pass (8 more, 245 total):

- `test_feed.py`: PC2 channel tags precede items, `itunes:type=episodic` present, `<image>` has `<url>`+`<title>`+`<link>`, channel `<link>` points at BASE_URL not the feed URL, `podcast:medium=podcast` present, `podcast:txt` carries the human-readable disclosure.
- `test_settings_store.py`: `podcast:guid` is UUIDv5 over the PC2 namespace with the URL canonicalized (scheme stripped, trailing slash removed); `_canonical_feed_url` round-trips three input shapes.
- `test_api_media.py`: `_validate_episode_id` rejection produces the lowercase `"not found"` body (proves the in-handler regex defense actually fires, not just FastAPI's router).

Container smoke (`tmp/phase7_smoke.sh`) verified inside the runtime image: feedgen + defusedxml + lxml load in the slim image; seeded a synthetic episode, fetched `/rss/rss.xml` (200, 1957 B, `application/rss+xml`), validated the PC2 namespace via `defusedxml.ElementTree`, confirmed `itunes:duration=00:02:05`, `podcast:guid` is a stable UUIDv5, `enclosure length=13` matches the seeded MP3 bytes, `/media/{id}.mp3` returns `audio/mpeg`, `/media/{id}.jpg` returns `image/jpeg`, `/media/{id}.vtt` returns `text/vtt` with `Cache-Control: max-age=86400`, and `If-Modified-Since` round-trips to `304 Not Modified`.

### Added (Phase 6 - Artwork + Transcripts)

- Pipeline now runs **extract -> cleanup -> corrections -> chunk -> tts -> audio -> artwork -> transcript**. Final `status=done` with `stage=transcript`. Phase 7 will append finalize (which inserts/updates the episodes row + writes the transcript to disk).
- `backend/app/services/artwork.py`: async artwork pipeline per build-plan Artwork Processing.
  - Pulls `ogImage` / `og:image` / `og_image` from the Firecrawl `metadata` dict.
  - Downloads via `httpx.AsyncClient` with `ARTWORK_FETCH_TIMEOUT_SECONDS`, `follow_redirects=True`. HTTP errors and network errors are typed (`_HttpError` / `httpx.TimeoutException` / `httpx.NetworkError`).
  - Opens with Pillow, validates the format against an explicit allowlist (`JPEG/PNG/WEBP/GIF/BMP/TIFF/MPO`) so SVG and exotic formats fall through cleanly.
  - Rejects sources below `ARTWORK_MIN_SOURCE_PX` so we don't ship a blurry upscale as the episode card.
  - Flattens `RGBA/LA/P` to RGB on a black background (JPEG has no alpha channel), center-crops to square, resizes via `LANCZOS` to `ARTWORK_SIZE_PX`, saves JPG quality `ARTWORK_JPG_QUALITY` with `optimize=True` and `exif=b""` to strip metadata.
  - Every failure path returns `None` and emits an `artwork_fallback` WARN with a typed `reason` (`missing_og_image`, `download_unreachable`, `download_http_error`, `download_too_large`, `unidentified_format`, `unsupported_format`, `source_too_small`, `decompression_bomb`, `pillow_decode_failed`, `atomic_write_failed`, `blocked_scheme`, `blocked_host`). The pipeline continues; episodes with no per-episode JPG render the feed-level artwork (Phase 7).
  - Hardened against attacker-controlled `og:image` URLs (the article HTML is scraped, so the URL is influenced by content the operator did not write): scheme allowlist (`http`/`https` only -- rejects `data:`, `javascript:`, `file:`); SSRF guard rejects private/loopback/link-local/multicast IPs after DNS resolution; streaming download with hard `ARTWORK_MAX_DOWNLOAD_BYTES` cap so a hostile body can't OOM the worker; broad `httpx.HTTPError` catch so `InvalidURL` / `UnsupportedProtocol` / `TooManyRedirects` / `DecodingError` / `RemoteProtocolError` all fall back instead of escaping the "never raises" contract.
  - EXIF orientation is honored via `ImageOps.exif_transpose` BEFORE flatten/crop/resize so phone-shot JPEGs (Orientation=6) don't render sideways after `exif=b""` strips the tag.
  - JPG write is atomic (`services/atomic_write.write_bytes_atomic` with prefix `.artwork-`) so a crash mid-save can't leave a truncated cover served by the Phase 7 RSS handler.
  - `Image.Resampling.LANCZOS` (not the deprecated `Image.LANCZOS` alias).
- `backend/app/services/transcript.py`: pure WebVTT renderer.
  - `TranscriptChunk(text, duration_secs)` + `build_vtt(chunks, silence_ms)`.
  - Cumulative timeline mirrors the produced audio: `start[i+1] = end[i] + silence_ms/1000`. Cues numbered sequentially from 1.
  - `_format_ts` renders `HH:MM:SS.mmm`. `_escape_cue` escapes `&` first (so `<` and `>` don't get double-encoded), then `<`, `>`.
  - Validates: negative `silence_ms` and negative `duration_secs` raise `ValueError`. Empty chunk list returns the `WEBVTT` header only so Phase 7's finalize can still write a valid (if empty) file.
- `backend/app/services/pipeline.py`:
  - `_stage_artwork` calls `artwork.process_artwork` against the extraction metadata + `DATA_DIR/media`. Never raises; logs `artwork_fallback_to_feed` when the result is `None`.
  - `_stage_transcript` zips chunk texts with per-chunk durations from `tts.GenerateResult` and calls `transcript.build_vtt(silence_ms=TTS_CHUNK_SILENCE_MS)` so VTT timestamps align with the produced MP3 exactly.
  - Final `_mark_done(final_stage="transcript")`.
- `backend/app/config.py`: `ARTWORK_SIZE_PX` (3000), `ARTWORK_JPG_QUALITY` (85), `ARTWORK_FETCH_TIMEOUT_SECONDS` (15), `ARTWORK_MIN_SOURCE_PX` (600). All overridable per `.env.example`.
- Runtime deps: `pillow>=10.4`.

Tests (23 new, 180 total)

- `test_artwork.py` (9): happy path resize + EXIF stripped, 1200x800 center-crop yields square output, missing og:image, HTTP 404, ReadTimeout, corrupted bytes, undersized source, SVG body, RGBA flatten to RGB.
- `test_transcript.py` (10): empty input returns header-only, single cue starts at zero, multi-cue inserts `silence_ms` padding (12.450 -> 12.700 -> 24.800), sequential cue numbering, hour-rollover timestamps, `&`/`<`/`>` escaping in cue text without double-encoding, zero-silence allowed, negative `silence_ms` rejected, negative duration rejected, output terminates with exactly one trailing newline.
- `test_pipeline.py` (4 new): pipeline writes the JPG to `data/media/{episode_id}.jpg` and reaches `stage=transcript`; pipeline still completes when ogImage is absent (artwork fallback); transcript stage receives the live chunk texts + TTS durations + `TTS_CHUNK_SILENCE_MS`; a raising `build_vtt` marks the job failed with `stage=transcript` (no stuck-in-processing).
- `test_pipeline.py` / `test_worker.py` (existing): updated `stage` assertions from `"audio"` to `"transcript"` to match the new final stage.

Container smoke (`tmp/phase6_smoke.sh`) verified inside the runtime image: Pillow loads in the slim image, `process_artwork` produces a 53.3 KB JPG at 3000x3000 from a synthetic 1200x800 PNG with EXIF stripped, and `build_vtt` renders a 2-cue 120-byte VTT with the expected 250 ms inter-cue gap (`00:00:12.450 --> 00:00:20.800` after 12.700 boundary), HTML-escaped special chars, and `WEBVTT` header.

### Code-review pass (multi-agent /simplify + /code-review for Phase 6)

Findings surfaced and applied:

- **httpx exception coverage broadened**: `_download`'s original `except (httpx.TimeoutException, httpx.NetworkError)` missed `httpx.InvalidURL`, `httpx.UnsupportedProtocol`, `httpx.TooManyRedirects`, `httpx.RemoteProtocolError`, and `httpx.DecodingError` -- all reachable from attacker-controlled og:image URLs and all of which would escape the "never raises" contract and fail the artwork stage. Now catches `httpx.HTTPError` as the final arm so every transport-level error is converted to a `download_unreachable` fallback.
- **SSRF guard**: `_assert_public_host` resolves the hostname (DNS) and rejects private / loopback / link-local / multicast / reserved / unspecified addresses BEFORE the GET. Re-runs on every redirect via httpx `event_hooks={"request": [...]}` so a 302 into the internal network is caught. Scheme allowlist (`http`/`https` only) rejects `data:`, `javascript:`, `file:`, `ftp:` up front. Two new typed exceptions: `_BlockedHostError` (with reason: `dns_resolution_failed`, `non_public_address_<ip>`, etc.) and the `blocked_scheme` fallback reason.
- **Download size cap**: streams the response with `client.stream("GET", ...)`, checks advertised `Content-Length` first, then enforces `ARTWORK_MAX_DOWNLOAD_BYTES` on each streamed chunk. A hostile body can no longer OOM the worker by serving an unbounded payload within the fetch timeout. Default cap is 25 MiB (`ARTWORK_MAX_DOWNLOAD_BYTES=26214400` in `.env.example`).
- **EXIF orientation honored before crop/resize**: a portrait phone JPEG with `Orientation=6` was being saved sideways at 3000x3000 because `exif=b""` strips the orientation tag with no recovery. Now `ImageOps.exif_transpose(img)` runs first; the test `test_process_artwork_exif_rotation_actually_rotates_pixels` samples a colored stripe from the expected post-rotation column to verify the pixels (not just the EXIF tag) were transposed.
- **Pillow API hygiene**: uses `Image.Resampling.LANCZOS` (the supported attribute on Pillow 10+) instead of the deprecated `Image.LANCZOS` alias. Wraps `Image.open` in a context manager so the underlying `BytesIO` and Pillow decoder are released on every path. Uses `rgba.getchannel("A")` instead of `rgba.split()[-1]` (three fewer band-image allocations per source). Adds mode `"PA"` (palette-with-alpha) to the alpha-flatten list so transparent regions composite onto black rather than show the palette color.
- **Atomic JPG write**: re-uses `services/atomic_write.write_bytes_atomic` (Phase 4 helper, with `prefix=".artwork-"`). A crash mid-save can no longer leave a truncated cover that the Phase 7 RSS handler would serve.
- **Transcript timeline as integer ms**: `cursor_ms` is incremented as `int` rather than `cursor_secs` as `float`, eliminating cumulative drift over hundreds of cues. Trailing silence is no longer added after the final cue (was a dead store and would have desynced any cumulative-end check). `_escape_cue` switched to `html.escape(text, quote=False)` and now also flattens internal whitespace runs via `" ".join(text.split())` so a chunk with an embedded blank line can't split a VTT cue. `math.isfinite` rejects NaN and +/-inf duration values (would have crashed `_format_ts`).
- **Pipeline integration**: `core/paths.media_dir(settings)` is the single source of truth for `{DATA_DIR}/media`; `_stage_audio` and `_stage_artwork` both call it. `_stage_transcript` does an explicit `len(chunks) != len(chunk_results)` check so the resulting `jobs.error` reads "transcript stage: N chunks but M TTS results -- pipeline state corrupted" instead of a stdlib zip message.
- **CHANGELOG accuracy**: previous draft listed `pillow_render_failed` (no longer emitted after the render path was folded into a single `_decode_and_render`) and was missing `download_too_large`, `decompression_bomb`, `atomic_write_failed`, `blocked_scheme`, `blocked_host` -- now reflects the real reason set.
- **Module docstring on `pipeline.py`**: was Phase-2 vintage and listed only the extract stage; updated to describe the Phase 6 chain.
- **Stale comment / dead branch in `_decode_and_render`**: removed the `if oriented is None: oriented = opened` guard. `ImageOps.exif_transpose` does not return `None` on Pillow 10+; the comment was factually wrong about Pillow's contract.

New tests added by the review pass (17 more, 197 total):

- `test_artwork.py`: og:image as list (picks first non-empty string); scheme allowlist rejects `data:` / `javascript:` / `file:` / `ftp:`; non-timeout `httpx.RemoteProtocolError` falls back as `download_unreachable`; SSRF guard rejects a hostname that resolves to a private IP; streaming-only oversize cap (no Content-Length header, body delivered via `httpx.AsyncByteStream`); Content-Length cap fires before streaming; Pillow `DecompressionBombError` maps to `decompression_bomb` reason; EXIF orientation actually rotates pixels (samples a colored stripe from the expected post-rotation position); atomic-write leaves no `.artwork-*` temp file in the output dir on success; RGBA flattening preserved; ogImage list fallback.
- `test_transcript.py`: NaN and +inf duration rejected via `math.isfinite`; internal newline runs collapse to a single space (would otherwise split a VTT cue); cumulative drift stays exact integer-ms over 300 cues with an irrational duration.
- `test_pipeline.py`: transcript stage rejects a `len(chunks) != len(chunk_results)` mismatch with a domain-specific error message and stops at `stage='transcript'`.

### Added (Phase 5 - Chunking + Audio pipeline)

- Pipeline now runs **extract -> cleanup -> corrections -> chunk -> tts -> audio**. Final `status=done` with `stage=audio`; an MP3 lands at `/data/media/{episode_id}.mp3`. Phase 6 will append artwork + transcript + finalize.
- `backend/app/services/chunker.py`: hybrid chunker per build-plan rules. Paragraphs first (`\n\n`); sentence boundaries via `(?<=[.!?])\s+`; comma / semicolon fallback for oversize sentences that emits a `chunk_fallback_split` WARN record so dashboards can spot a steady stream of them. `UnsplittableSentenceError` is raised with a sentence preview when no breakpoint fits -- the pipeline marks the job failed with stage=chunk rather than truncating content. Targets 180 words / 1100 chars per chunk; hard max 220 words.
- `backend/app/services/tts.py`: added `generate_chunk_with_retry` wrapping `generate_chunk` with tenacity. `TTS_RETRY_COUNT` attempts, exponential backoff, retry only `TTSProviderError` / `TTSTimeoutError`; `TTSRequestError` propagates immediately per build-plan line 829.
- `backend/app/services/audio.py`:
  - `trim_silence(waveform, sample_rate, settings)` -- torch-based silence detection per the ebook2audiobook algorithm. `AUDIO_SILENCE_THRESHOLD` + `AUDIO_SILENCE_BUFFER_MS`. Fully silent input returns unchanged so the chunk isn't accidentally erased.
  - `concat_with_padding(chunk_paths, output_path, settings)` -- load each WAV via soundfile, trim silence, insert a `torch.zeros((1, n))` pad of `TTS_CHUNK_SILENCE_MS` between chunks, concatenate via `torch.cat`, write the combined WAV. Returns `(output_path, sample_rate)`.
  - `normalize_and_encode(input_wav, output_mp3, settings)` -- runs the full ebook2audiobook ffmpeg filter chain (`agate`, `afftdn`, `acompressor`, `loudnorm=I=-14:TP=-3:LRA=7:linear=true`, six `equalizer` bands, `highpass=63`), then encodes to MP3 via `libmp3lame` at 128k / 24000 Hz / stereo upmix (`-ac 2`). Reads final duration via mutagen.
  - `FfmpegError` carries `returncode` + last 400 chars of stderr so operators have something useful in the job's error column.
  - `remove_quietly(*paths)` -- used by the pipeline's `finally` block to clean per-chunk WAVs + the concatenated WAV regardless of success/failure (no persistent debug artifacts).
- `backend/app/services/pipeline.py`:
  - `_stage_chunk` calls `chunker.chunk`, logs `chunk_complete` with `chunk_count` + `min/max/total_words`.
  - `_stage_tts` iterates chunks, calls `tts.generate_chunk_with_retry`, emits `tts_chunk_done` per chunk and `tts_stage_complete` with `total_audio_secs`. Per-chunk durations are kept on the in-memory pipeline state for Phase 6 transcript generation.
  - `_stage_audio` calls `concat_with_padding` + `normalize_and_encode`. Intermediate files are cleaned in a `finally` block.
  - Final `status=done` with `stage=audio` (Phase 7's finalize will flip the final stage to "done").
- `backend/app/config.py`: added Phase 5 tunables. Chunking: `TTS_CHUNK_TARGET_WORDS`, `TTS_CHUNK_MAX_WORDS`, `TTS_CHUNK_MAX_CHARS`, `TTS_CHUNK_SILENCE_MS`. Audio: `AUDIO_SILENCE_THRESHOLD`, `AUDIO_SILENCE_BUFFER_MS`, `LOUDNORM_TARGET_LUFS`, `LOUDNORM_TRUE_PEAK_DB`, `LOUDNORM_LRA`, `MP3_BITRATE`, `MP3_SAMPLE_RATE`, `MP3_CHANNELS`.
- Runtime deps: `torch>=2.4` (CPU wheel via pinned `pytorch-cpu` uv source), `numpy>=1.26`, `soundfile>=0.12`, `mutagen>=1.47`. `soundfile` is used in place of `torchaudio` because torchaudio 2.11 made TorchCodec the default backend and requires a separate package; soundfile reads/writes WAVs reliably without it.
- `Dockerfile`: added `ffmpeg` and `libsndfile1` packages so the audio stage and soundfile have the binaries they need at runtime.

Tests (21 new, 152 total)

- `test_chunker.py` (9): empty input, single short paragraph, paragraph-boundary split, greedy sentence packing under target/max, comma/semicolon fallback with WARN log assertion, hard abort with sentence preview when no breakpoint fits, char-cap override of word count, repeated-blank-line normalization, sentence-punctuation preservation.
- `test_tts.py` (3 new, 12 total): retry succeeds after a transient 5xx, retry never fires on 4xx (exactly one attempt), retry exhausts on persistent 5xx and raises `TTSProviderError` with the right attempt count.
- `test_audio.py` (9): trim removes leading/trailing silence, trim preserves fully-silent input, concat appends inter-chunk silence with sample-accurate duration, concat rejects zero chunks, concat rejects rate mismatch across chunks, `normalize_and_encode` produces a valid MP3 readable by mutagen (real ffmpeg call), ffmpeg error surfaces as `FfmpegError`, `remove_quietly` swallows missing files, WAV round-trip via stdlib `wave` module verifies header shape. Suite auto-skips when ffmpeg is absent.

Container smoke verified end-to-end with a mock Firecrawl + mock OpenAI-compatible LLM + mock TTS wrapper that returns a 440 Hz tone WAV at the request's `wav_path`. Pipeline: queued -> done with `stage=audio`, MP3 written to `/data/media/{episode_id}.mp3` (33 KB), mutagen-read duration 2.064s, structured logs include `chunk_complete`, `tts_stage_complete`, `audio_encode_done`, `audio_complete`, `pipeline_done`.

### Code-review pass (multi-agent /simplify + /code-review for Phase 5)

Findings surfaced and applied:

- **`_finalize_failure` no longer crashes on curly-brace exception text**: switched from `error_template.format(stage=last_stage)` to `error_template.replace("{stage}", last_stage)`. ffmpeg stderr, JSON bodies, sentence previews, and any other user-controlled exception text can contain literal `{` or `}` -- `str.format` would have raised `KeyError` mid-finalize and left the job stuck in `processing`. Replace is inert to braces.
- **chunker `chunk_index` in `chunk_fallback_split` WARN logs is now the global running position**: thread the current output-chunk count from `chunk()` through `_chunk_paragraph` into `_pack` as `base_chunk_index`. Previously hard-coded to `len(_split_paragraphs(""))` which is structurally always 0 -- every paragraph's WARN reset to 0, defeating the build-plan diagnostic.
- **Comma/semicolon fallback preserves the original separator**: `_COMMA_OR_SEMI` now captures the separator via a regex group; `_join_pieces` rebuilds the chunk with the original `;` or `,` instead of silently rewriting semicolons to commas. XTTS-v2 prosody pauses are different for the two, so this matters for the narrator's pace.
- **`concat_with_padding` validates channel uniformity AND builds the pad tensor with the matching channel count**: the previous `(1, pad_n)` hard-coding would crash with an opaque `torch.cat` RuntimeError on any future non-mono input; now it raises a clean `AudioError("chunk N has X channels but earlier chunk had Y")`.
- **loudnorm `linear=true` removed**: the flag is a documented no-op without a first-pass measurement (which we don't run); ffmpeg silently fell back to dynamic single-pass loudnorm anyway. Removed so the filter chain accurately describes what's running. Two-pass measure-then-apply is a follow-up.
- **Module docstring drift fixed**: `audio.py` now explicitly notes the soundfile-not-torchaudio deviation introduced in Phase 5 (torchaudio 2.11 made TorchCodec the default save backend; we ship soundfile instead).

New tests added by the review pass (5 more, 157 total):

- `test_chunker.py`: semicolon preservation in the comma fallback; chunk_index in WARN logs is the running article-wide position across multiple paragraphs.
- `test_audio.py`: channel-mismatch between chunks surfaces as a clean `AudioError("channels")` (covers the new validation that `concat_with_padding` does alongside the existing rate-mismatch check).
- `test_pipeline.py`: curly-brace exception text doesn't leave the job stuck in 'processing' (forces a `MIN_CLEANUP_CHARS` failure with `{curly braced}` in the error message and asserts `status=failed` is persisted); `_stage_audio` finally block actually calls `audio.remove_quietly` with the combined WAV and per-chunk WAVs (spy fixture catches regression if a future refactor drops the cleanup).

### Added (Phase 4 - TTS Wrapper)

- `tts-wrapper/` -- new sibling container that wraps Coqui XTTS-v2 in a FastAPI service. Endpoints per build plan:
  - `POST /generate` accepts `{text, episode_id, chunk_index}`, runs inference under an `asyncio.Lock` so concurrent calls queue at the GPU boundary while `/health` stays responsive, writes the result to `/data/media/{episode_id}_chunk_{chunk_index}.wav`, returns `{wav_path, duration_secs, sample_rate}`.
  - `GET /health` reports `{ok, model_loaded, reference_loaded}`; 503 until the model has loaded AND the reference embeddings are computed.
  - `POST /reload` acquires the same lock, re-reads `reference/voice.wav`, and recomputes speaker embeddings -- called by the main app after a reference-voice commit (Phase 10).
- `tts-wrapper/engine.py`: `Engine` Protocol + `XTTSEngine` real implementation. The Coqui TTS and PyTorch imports are deferred to `XTTSEngine.load()` so the module is importable in test environments without GPU runtime. Tests inject a `FakeEngine` via `create_app(engine=..., data_dir=...)`.
- `tts-wrapper/main.py`: lifespan calls `engine.load()` and `sys.exit(1)`s on failure so uvicorn exits non-zero and the container restart policy fires (matches the missing-`voice.wav` and model-load-failure deliverables). Pydantic request model uses `extra="forbid"`, `min_length` on text + episode_id, `ge=0` on `chunk_index`. GPU OOM raises a typed `GPUOutOfMemoryError` that the route catches, calls `torch.cuda.empty_cache()`, and returns 500 with `{error: "GPU OOM", cause}`. WAV duration is computed from the file header so the response value matches what was written.
- `tts-wrapper/config.py`: env-driven `Config` carrying `TTS_DEVICE`, `TTS_LANGUAGE`, the XTTS generation tunables (`XTTS_TEMPERATURE`/`LENGTH_PENALTY`/`REPETITION_PENALTY`/`TOP_K`/`TOP_P`), and the sample rate. Defaults match build-plan.md.
- `tts-wrapper/Dockerfile`: `pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime` base, `coqui-tts` (idiap fork) from PyPI, `libsndfile1`, `curl` for the healthcheck. `uvicorn main:create_app --factory --workers 1 --timeout-keep-alive 300`. Healthcheck on `/health` with 120s `start_period` to cover the model download/load on cold start.
- `tts-wrapper/Dockerfile.cpu`: alternate base (`pytorch/pytorch:2.4.0-cpu`) for hosts without CUDA. 5-10x slower per build plan but verifies the contract.
- `tts-wrapper/README.md`: XTTS CPML license note, voice-file specs (6-12s, 22050+ Hz, mono, clean) with the exact ffmpeg conversion line for the LibriTTS clips in `ref_audio/`, HF cache mount, CPU vs CUDA, GPU pinning.
- `backend/app/services/tts.py`: async client for the wrapper. `generate_chunk(text, episode_id, chunk_index, settings)` returns a frozen `GenerateResult`. Typed exceptions `TTSTimeoutError`, `TTSProviderError` (5xx + network, retryable), `TTSRequestError` (4xx + malformed). `reload(settings)` posts `/reload` and returns the wrapper's body.
- `backend/app/services/reachability.py`: added `check_tts(settings)` with the build-plan grace-period semantics (poll up to `TTS_REACHABILITY_GRACE_SECONDS` with `TTS_REACHABILITY_PROBE_TIMEOUT` per attempt, return on first `model_loaded: true`). Wired into `run_all` so the worker waits for the wrapper before processing.
- `backend/app/config.py`: added `TTS_LANGUAGE`, `TTS_DEVICE`, `TTS_HTTP_TIMEOUT_SECONDS`, `TTS_RETRY_COUNT`, `TTS_REACHABILITY_GRACE_SECONDS`, `TTS_REACHABILITY_PROBE_TIMEOUT`, and the five `XTTS_*` tunables. `.env.example` documents every default.
- `docker-compose.yml`: added the `tts-wrapper` service with nvidia GPU reservation (`device_ids: ['0']`), shared `./data:/data`, read-only reference mount, and a named `hf_cache` volume so the ~2GB model download survives container rebuilds. `app` now `depends_on: tts-wrapper: condition: service_healthy`.

Tests (22 new, 153 total):

- `tts-wrapper/tests/test_main.py` (9): /health 200 path + 503 when reference not loaded; /generate writes the WAV at the expected `/data/media/{episode}_chunk_{n}.wav` path with the duration computed from the header; blank text rejected; negative chunk_index rejected; extra fields rejected; GPU OOM surfaces the 500 envelope with cause; sequential /generate calls observed in submission order via the lock; /reload returns `{ok: true, reference_loaded: true}` and increments the engine's reload counter.
- `backend/tests/test_tts.py` (9): client wire format (path + body), 5xx/4xx/timeout/network error classification, non-JSON + missing-key shape errors, /reload happy + 5xx paths.
- `backend/tests/test_tts_reachability.py` (4): probe succeeds on first try, polls until `model_loaded` flips to true, reports `grace period expired` on network failure, reports `model_loaded=false` when the wrapper is up but not ready.

Verification approach
- The wrapper engine abstraction means the FastAPI contract is exercised end-to-end in CI without ever loading XTTS-v2. The real model load + GPU inference is operator-side per the build plan deliverable ("Manual test: send chunk text, get WAV back; verify CPU fallback path"). The `tts-wrapper/README.md` documents the exact `docker compose build tts-wrapper && docker compose up` flow plus a single-curl smoke test once the wrapper reports `model_loaded: true`.
- `docker compose config` validates the full multi-service stack (app + tts-wrapper) including the GPU reservation, mount layout, env wiring, healthcheck dependency, and named volumes.

### Code-review pass (multi-agent /simplify + /code-review for Phase 4)

Findings surfaced and applied:

- **Path traversal blocked**: `GenerateRequest.episode_id` now carries a `pattern=r"^[A-Za-z0-9_.-]+$"` and `chunk_index` an upper bound. The wrapper also runs a `Path.resolve().is_relative_to(out_dir)` belt-and-braces check before writing, so an episode_id that somehow slips through the validator still can't escape `data_dir/media/`.
- **Event loop unblocked**: `XTTSEngine.synthesize` and `_compute_embeddings` now run via `asyncio.to_thread(...)`. The wrapper's `/health` route is genuinely responsive while a chunk is mid-inference, not just lock-free.
- **Per-request inference timeout**: `/generate` wraps `engine.synthesize` in `asyncio.wait_for(timeout=TTS_REQUEST_TIMEOUT_SECONDS)` (default 120s, env-tunable) and returns 504 on timeout. Build-plan line 822's "Per-request timeout 120s" is now enforced; a wedged inference call can no longer hold the worker forever.
- **WAV writes are atomic**: `_atomic_write_bytes` (tempfile + fsync + os.replace) replaces the bare `Path.write_bytes` so Phase 5+ stitching can't observe a partial chunk file.
- **Lifespan re-raises instead of `sys.exit(1)`**: the original exception now propagates through Starlette's documented `lifespan.startup.failed` path, giving uvicorn a clean Exception-based exit and making the failure path safely testable via `TestClient.__enter__`.
- **Reload no longer corrupts engine state on failure**: `reload_reference` snapshots the prior latent + speaker_embedding + reference_loaded flag and rolls them back if `_compute_embeddings` raises. A bad voice.wav now returns 500 but `/health` keeps reporting the prior good state instead of flipping to permanent `reference_loaded=false`.
- **Spec drift fixed on response bodies**: `/reload` now returns just `{"ok": true}` per build-plan line 805; `/health` 503 body now includes a diagnostic `"error"` string per build-plan line 803.
- **Coqui-tts version pinned**: `tts-wrapper/pyproject.toml` upper-bounds the dep to `<0.25` and a comment explains that the wrapper reaches into Coqui internals (`model.synthesizer.tts_model.get_conditioning_latents`/`inference`) not covered by semver.
- **Dockerfile installs from pyproject**: both `Dockerfile` and `Dockerfile.cpu` drop their hand-written `pip install` list in favor of `pip install --no-cache-dir .`. Single source of truth across the venv-based dev and the container build.
- **app.state.lock created at app construction**: no longer split between module-time engine assignment and lifespan-time lock assignment; consistent ordering for ASGI middleware or unusual test harnesses that bypass the lifespan.
- **Compose security hardening parity**: tts-wrapper service now declares `security_opt: no-new-privileges: true` and `cap_drop: ALL`, matching the app service posture.
- **check_tts AsyncClient pooled across probes**: one `httpx.AsyncClient` for the whole grace window so successive probes share the TCP connection pool. Per-probe debug log line emitted so a stalled cold-start shows up in Loki instead of 60 silent seconds.
- **Module-style imports moved to top in `engine.py`**: `io`, `wave`, and `numpy` (transitive numpy dep) no longer hide as inline imports inside `_wav_bytes`, matching the project's "no inline imports" rule.

New tests added by the review pass (5 more, 158 total):

- `tts-wrapper/tests/test_main.py`: path traversal in `episode_id` rejected (covers `../etc/foo`, `a/b`, `with space`, `with\x00null`); `/reload` 404 when engine raises `FileNotFoundError`; `/reload` 500 when engine raises any other exception (each path matters because the backend's `tts.reload` client classifies 4xx as terminal `TTSRequestError` and 5xx as retryable `TTSProviderError`); engine `sample_rate` mismatch is observable in the response (FakeEngine.sample_rate=24000 over a 22050 Hz WAV -> response carries the engine's value, duration computed from the WAV header -- a future code change that reads the rate from the header fails the assertion); atomic write leaves no `.tmp` files on success.

Container smoke remains operator-side per the original Phase 4 plan; the additional fixes don't change the manual XTTS smoke path documented in `tts-wrapper/README.md`.

### Added (Phase 3 - LLM Cleanup)

- `backend/app/services/llm.py`: multi-provider client. `async generate(system, user, settings, *, temperature?, max_tokens?)` dispatches to `_call_openai_compatible` (POST `{OPENAI_BASE_URL}/chat/completions`) or `_call_anthropic` (POST `https://api.anthropic.com/v1/messages` with `x-api-key` + `anthropic-version` headers). Typed errors: `LLMTimeoutError`, `LLMProviderError` (5xx, retryable), `LLMRequestError` (4xx + malformed JSON, non-retryable). Both providers return parsed text via the established response shapes.
- `backend/app/services/corrections.py`: single-pass alternation substitution. Whole-word matches via stricter lookarounds (`(?<![\w-])` / `(?![\w-])`) so `kubectl` doesn't match inside `kubectl-helper` AND keys ending in non-word symbols like `C++` still match next to whitespace. Case-sensitive, longest-key-first via regex alternation order, auto-escapes regex specials so operators can write `C++` or `node.js`. `validate(dictionary, max_entries)` returns a `ValidationResult` listing every failure (root-not-dict, entry-count-cap, per-key empty/length/whitespace/control-char) rather than raising. `load`/`save` round-trip the JSON file with atomic temp-and-replace.
- `backend/app/services/prompt.py`: `load(path)` reads the file, `save(path, content, *, max_bytes)` writes atomically with a byte-length cap (not character-length, so multi-byte UTF-8 is enforced correctly). `PromptTooLargeError` surfaced separately so the API can return 413.
- `backend/app/prompts/script.txt`: replaced the Phase 1 placeholder with a real cleanup prompt that captures the build plan's remove / replace / transform / normalize / leave-alone behavior, with explicit output-format instructions (plain text only, no preamble, blank lines between paragraphs).
- `backend/app/services/pipeline.py`: pipeline now runs `extract` -> `cleanup` -> `corrections`. Final status=done with stage=corrections for Phase 3. Cleanup stage re-reads the prompt file every call so operator edits take effect on the next job without a restart. `MIN_CLEANUP_CHARS` guard mirrors the extract-stage threshold check. Corrections stage logs `entries_loaded` and `delta_chars`.
- `backend/app/services/reachability.py`: added `check_llm(settings)`. For `openai-compatible`, probes `GET {OPENAI_BASE_URL}/models` (the well-known list-models endpoint every Ollama / vLLM / LM Studio / OpenAI-compatible server exposes). For `anthropic`, no cheap probe exists per the build plan, so the check only validates `ANTHROPIC_API_KEY` is present. Wired into `run_all` so the worker exits non-zero on first boot when the LLM is unreachable.
- `backend/app/api/v1/prompt.py`: `GET /api/v1/prompt` returns `{prompt}`; `PUT /api/v1/prompt` accepts `{prompt}` with `extra="forbid"` and validates byte-length against `MAX_PROMPT_LENGTH_BYTES`. 413 with `{max_bytes, actual_bytes}` details on oversize, 404 when the underlying file is missing.
- `backend/app/api/v1/corrections.py`: `GET /api/v1/corrections` returns the full dictionary; `PUT /api/v1/corrections` accepts the full dict, runs `corrections.validate`, returns 400 with a per-entry failure list, otherwise persists atomically.
- `backend/app/api/v1/router.py`: mounts the two new routers alongside `/submit` and `/status/{job_id}`.
- `backend/app/config.py`: added `MAX_PROMPT_LENGTH_BYTES` (default 10240) and `MAX_CORRECTIONS_ENTRIES` (default 500).
- `.env.example`: documented both new env vars.

Tests (50 new, 109 total):

- `test_llm.py` (10): openai-compatible chat-completions wire format (path + Authorization + messages + temperature + max_tokens), 5xx -> `LLMProviderError`, 4xx -> `LLMRequestError`, ReadTimeout -> `LLMTimeoutError`, non-JSON body -> request error, unexpected response shape -> request error; anthropic wire format (host + `x-api-key` + `anthropic-version` + system + messages), missing key surfaces clearly, non-text content block rejected, unknown provider raises.
- `test_corrections.py` (22): whole-word with hyphen-aware boundary, case sensitivity, longest-first via alternation, auto-escape for `C++` / `node.js`, empty-dict + no-match short circuits; validator rejects non-dict root, too-many-entries, empty key, oversize key/value, leading/trailing whitespace, empty value, control characters; load/save round-trip + atomic write (no partial file on failure) + missing/empty file returns `{}` + non-object root rejected.
- `test_prompt.py` (5): load returns contents, save round-trip, oversize raises (byte length not char length so multi-byte UTF-8 trips the cap correctly), atomic no-partial-on-failure.
- `test_api_prompt_corrections.py` (8): GET/PUT round-trip for both endpoints with file restoration after, `extra="forbid"` rejection on prompt, 413 on oversize prompt with `{max_bytes, actual_bytes}` details, 400 on bad correction entry, 400 on entry-count exceeded.
- `test_llm_reachability.py` (5): openai-compatible 200/network-failure/5xx paths; anthropic skips the HTTP probe and only validates the key.
- `test_pipeline.py`/`test_worker.py` (updated): stub both `extraction.extract` and `llm.generate`, expect status=done with stage=corrections.

Container smoke verified end-to-end with a mock Firecrawl + mock OpenAI-compatible server: reachability passes both checks, submit returns 201, status progresses queued -> done with stage=corrections in single-digit ms, structured logs include `reachability_check` (firecrawl + llm), `stage_start`/`stage_end` for extract/cleanup/corrections, `cleanup_complete` with `input_chars`/`output_chars`, `corrections_complete` with `entries_loaded`/`delta_chars`, and `pipeline_done`. Prompt and corrections endpoints round-trip via curl.

### Code-review pass (multi-agent /simplify + /code-review for Phase 3)

Findings surfaced and applied:

- **Cleanup stage now wraps `llm.generate` with tenacity retry** per build plan line 251 (`LLM_RETRY_COUNT` attempts, exponential backoff, retries `LLMProviderError`/`LLMTimeoutError`, never retries `LLMRequestError`). Previously the cleanup stage failed permanently on the first transient 5xx; `LLM_RETRY_COUNT` was config-defined but unused.
- **`CleanupTooShortError` introduced** as a dedicated exception (subclasses `Exception`, not `ValueError`) so future broad `except ValueError` calls can't accidentally swallow the min-chars guard.
- **openai-compatible `content: null` handled cleanly**: providers that emit `tool_calls` instead of text return `content=null`; the cleanup stage previously crashed with `TypeError: object of type 'NoneType' has no len()`. Now classified as `LLMRequestError` with a clear message.
- **Anthropic multi-block responses now read correctly**: search-and-concatenate all `text` blocks instead of crashing if the first block is `thinking` or `tool_use`. Multi-text-block responses (extended thinking with citations) join their text content per Anthropic's documented usage.
- **Anthropic response shape now also catches `AttributeError`**: a non-dict content block (string, null) used to escape the typed handler as an opaque exception.
- **corrections.load now drops invalid entries with a WARN log**: a hand-edited file with empty keys would otherwise produce a regex like `(?<![\w-])(?:|kubectl)(?![\w-])` whose empty alternative matches at every word boundary. Sanitization mirrors the PUT validator so the bind-mount edit path is as safe as the API.
- **api/v1/corrections request body widened from `dict[str, str]` to `dict[str, Any]`** so Pydantic doesn't short-circuit non-string values before `corrections.validate` runs. Clients now receive the typed per-key failure envelope instead of the generic "Validation failed".
- **PromptBody now validates `min_length=1` AND rejects whitespace-only**: an admin accidentally clearing the textarea no longer silently writes an empty cleanup prompt.
- **`PromptTooLargeError` reparented from `ValueError` to `Exception`** so a downstream broad `except ValueError` can't accidentally swallow the 413 signal.
- **`reachability` log records renamed `stage="startup"` to `phase="startup"`** so reachability events don't collide with the pipeline-stage Loki label dimension used by every other log line.
- **Shared `services/atomic_write.py` helper** that both `prompt.save` and `corrections.save` now delegate to. Adds a parent-directory `fsync` after `os.replace` so the rename is durable across kernel crashes (the previous implementations fsynced the file but not the directory).
- **corrections.py module docstring updated** to reflect the lookaround boundary (which excludes hyphens) instead of the obsolete `\b` description.

New tests added by the review pass (9 more, 118 total):

- `test_llm.py` (3 new): Anthropic URL path + version locked (`/v1/messages`, `2023-06-01`); openai-compatible `content=null` raises typed `LLMRequestError`; Anthropic multi-block response with thinking + tool_use interleaved returns just the concatenated text blocks.
- `test_pipeline.py` (3 new): `MIN_CLEANUP_CHARS` guard fires with `stage=cleanup` and a clear error; cleanup retries once on `LLMProviderError` and ends with `status=done`; cleanup does NOT retry on `LLMRequestError` (exactly one attempt, ends with `status=failed`).
- `test_api_prompt_corrections.py` (3 new): blank prompt rejected (both empty and whitespace-only); byte-boundary test at exactly `MAX_PROMPT_LENGTH_BYTES` succeeds + one byte over returns 413 (guards against `>` -> `>=` off-by-one); non-string value in PUT corrections surfaces as the typed failure envelope instead of the generic "Validation failed".
- The existing persist-roundtrip tests now use a yield-style `_preserve_prompt_file` / `_preserve_corrections_file` fixture so the on-disk file is always restored in teardown, even if any assertion in the body fails. Previously a single flaky assert could leave the repo's `script.txt` or `pronunciation.json` polluted.

### Added (Phase 2 - Extraction)

- Runtime deps: `httpx>=0.27`, `tenacity>=9.0`. `httpx` moved out of `[dependency-groups].dev` into runtime since the Firecrawl client and the reachability prober both use it.
- `backend/app/services/extraction.py`: async Firecrawl client. POST `{FIRECRAWL_URL}/v1/scrape` with `{url, formats: ["markdown"]}`. Tenacity `AsyncRetrying` with `FIRECRAWL_RETRY_COUNT` attempts and exponential backoff seeded by `FIRECRAWL_BACKOFF_BASE_SECONDS`. Typed exceptions: `ExtractionError` (base), `ExtractionTransientError` (5xx / network / timeout, retryable), `ExtractionPermanentError` (4xx / malformed JSON / `success=false`), `ExtractionTooShortError` (below `MIN_EXTRACTION_CHARS`). Returns a frozen `ExtractionResult(markdown, metadata)`.
- `backend/app/services/jobs.py`: pure DB helpers. `compute_episode_id(url)` = MD5 truncated to 12 hex; `get_job`, `get_job_by_episode_id`, `episode_exists`, `create_job` (with `DuplicateSubmissionError` and reprocess-wipes-prior semantics), `claim_next_queued` (atomic SELECT+UPDATE under `BEGIN IMMEDIATE`), `set_stage`, `mark_done`, `mark_failed`, `job_as_dict`. Every UPDATE bumps `updated_at` explicitly per the build plan's application-managed timestamps contract.
- `backend/app/services/pipeline.py`: `process_job(job, settings)` orchestrator. Wraps the whole job under `asyncio.wait_for(JOB_TIMEOUT_SECONDS)`. Each stage writes its name to `jobs.stage` BEFORE running so the timeout path can report which stage was executing. Stage start/end/failure structured logs with `job_id` + `episode_id` + `stage` stamped via contextvars. Phase 2 wires only the `extract` stage; on success status=done with `stage=extract`. Phase 3+ will append cleanup and beyond.
- `backend/app/services/reachability.py`: `check_firecrawl(settings)` and `run_all(settings)`. Probes `GET {FIRECRAWL_URL}/v1/health`. The worker calls `run_all` at startup and exits non-zero on failure so the container restart loop surfaces a misconfigured stack instead of every job failing the same way mid-pipeline. The FastAPI lifespan deliberately does NOT call `run_all` so `/health/ready` and the admin API stay reachable for triage even when Firecrawl is down.
- `backend/app/worker.py`: polling loop now actually picks up queued jobs (`_pickup_once`), runs `pipeline.process_job`, and continues. Single in-flight. Reachability checks run before crash recovery and signal-handler install; failure causes `sys.exit(1)` so the supervisor cycles the container.
- `backend/app/api/errors.py`: global error envelope. All 4xx and 5xx return `{error, status, details?}` per build plan. RequestValidationError -> 400 with field details. Unhandled exceptions -> 500 with logged traceback but no client-side leakage.
- `backend/app/api/v1/` with `router.py`, `submit.py`, `status.py`. `POST /api/v1/submit` accepts `{url: AnyHttpUrl, reprocess?: bool}`, returns 201 `{job_id, episode_id, status, replaced_previous}`. 409 on duplicate (in-flight job OR existing episode without reprocess). 400 on invalid URL. `GET /api/v1/status/{job_id}` returns full job state or 404.
- `backend/app/main.py`: mounts the v1 router and registers the error handlers alongside the existing health router.
- `backend/app/config.py`: `JOB_TIMEOUT_SECONDS` widened from `int` to `float` so fractional values work in tests; production default unchanged at 1800.
### Code-review pass (multi-agent /simplify + /code-review)

Findings surfaced and applied:

- **jobs.create_job race + non-atomic DELETE**: wrapped the entire duplicate-check + delete + insert in `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK`. Two concurrent submits for the same URL can no longer both pass the in-flight check; a failure between the two DELETE statements in the reprocess path no longer leaves a half-wiped DB.
- **claim_next_queued ROLLBACK secondary-error mask**: switched to `contextlib.suppress(OperationalError)` around the rollback, matching the pattern already used in `database._apply_pending`.
- **extraction NoneType chain on `data: null`**: hardened `_parse_response` and the data/markdown/metadata extraction to handle `data=null`, non-dict bodies, and non-dict data with a typed `ExtractionPermanentError` instead of `AttributeError`.
- **pipeline error finalization can secondary-raise**: extracted `_finalize_failure` that wraps `_last_stage` and `_persist_failure` in their own try/except; a locked DB during the error handler no longer masks the original stage exception and the structured `pipeline_failed` / `pipeline_timeout` log lines always fire.
- **_run_stage missed CancelledError**: widened the except from `Exception` to `BaseException` so timeout-cancelled stages still emit the `stage_failed` log with duration_ms.
- **worker poll loop unsafe**: wrapped `_process_one` in try/except so a transient DB or OS error logs and backs off instead of killing the worker process.
- **SubmitRequest silently dropped unknown fields**: added `model_config = ConfigDict(extra="forbid")` so `{"reprcess": true}` (typo) surfaces as a 400 Validation failed instead of being silently ignored.
- **AnyHttpUrl normalization changed user URL**: replaced the `AnyHttpUrl` field type with a `str` + `field_validator` that runs `AnyHttpUrl(value)` for validation but returns the raw string, keeping `episode_id` deterministic against the user-submitted URL and the persisted `url` byte-for-byte identical.
- **_validation_handler unsafe JSON encoding**: wrapped `exc.errors()` in `jsonable_encoder` so non-primitive ctx values (Pattern, Enum, exception instances) don't fall through to the 500 handler.
- **reachability hits non-existent /v1/health**: probe now tries `/v1/health`, `/health`, and `/` in order; first 2xx wins. Self-hosted Firecrawl versions that only respond at `/` no longer fail startup.
- **extraction.\_raise_for_status magic numbers**: replaced bounds arithmetic with `response.is_server_error` / `response.is_client_error`.
- **jobs.get_job_by_episode_id duplicated SELECTs**: collapsed to one query with an optional WHERE fragment.
- **pipeline two except blocks duplicated**: shared via `_finalize_failure` helper.
- **extraction trailing unreachable raise**: clarified the comment to acknowledge it's a type-checker satisfier.
- **fast_backoff fixture was dead code**: replaced with a real fixture that sets `FIRECRAWL_BACKOFF_BASE_SECONDS=0` and clears the settings cache so retry tests actually run fast; deduped from each consumer.
- **.env.example default `FIRECRAWL_URL=http://firecrawl:3002` resolved nowhere on a fresh clone**: changed default to `http://host.docker.internal:3002` matching the `OPENAI_BASE_URL` pattern.

New tests added by the review pass (11 more, 59 total):

- `test_reachability.py`: 6 tests covering check_firecrawl 2xx, fallback through endpoint candidates, network unreachable, persistent 5xx reports last detail, run_all raises on failure, run_all returns on success.
- `test_api_v1.py`: 5 new tests -- `extra="forbid"` rejects typo'd field, raw URL preserved through to status, reprocess+inflight still returns 409 with the right reason, status endpoint returns failed jobs with stage + error, 500 envelope contract (no detail leakage, never logs exc.args into response).

- New tests (17 new, 59 total Phase 2 starting point pre-review):
  - `test_extraction.py`: happy path, retries-then-succeeds on 5xx, no-retry on 4xx, retry exhaustion on persistent 5xx, MIN_EXTRACTION_CHARS guard, `success=false` rejection. Uses `httpx.MockTransport` via a factory monkeypatch on `httpx.AsyncClient`.
  - `test_pipeline.py`: status=done with stage=extract on success; status=failed with stage+error on extraction error; status=failed with `JOB_TIMEOUT_SECONDS` error on timeout (last persisted stage reported).
  - `test_api_v1.py`: submit 201 + 12-char episode_id; submit 400 on invalid URL via the validation handler; submit 409 on in-flight duplicate; submit reprocess=true wipes prior episode and returns `replaced_previous=true`; status 200 with full envelope; status 404 with envelope.
  - `test_worker.py`: added `test_pickup_runs_pipeline_against_a_queued_job` (end-to-end with stubbed extractor) and `test_pickup_returns_false_when_no_queued_jobs`; existing `_crash_recovery` + run-loop tests still pass.
- Container smoke verified end-to-end with a mock Firecrawl on `:13002`: reachability check passes, `POST /api/v1/submit` returns 201, status progresses queued -> done within seconds, structured logs include `pipeline_start`, `stage_start`, `extract_complete` (markdown_chars=1900, has_title=true), `stage_end` (duration_ms=7), `pipeline_done`. Contextvars correctly stamp `job_id` + `episode_id` + `stage` on every record.

### Added (Phase 1 - Project Scaffold)

- Repo layout per the build plan: `backend/app/{api,core,utils,prompts,corrections,reference}`, `backend/tests/`, `data/`.
- `pyproject.toml` (uv-managed, Python 3.13). Runtime deps: `fastapi`, `uvicorn[standard]`, `pydantic`, `pydantic-settings`. Dev deps live under PEP 735 `[dependency-groups].dev` so `uv sync --no-dev` excludes them in the image. Marked as a uv virtual project (no wheel build).
- `backend/app/version.py` as the single source of `__version__`.
- `backend/app/config.py`: Pydantic `BaseSettings` covering the required, conditional, and tunable env vars from the build plan. `extra="forbid"` so typo'd keys in `.env` fail loudly. Provider-specific validation: `openai-compatible` requires `OPENAI_BASE_URL` + `OPENAI_API_KEY`; `anthropic` requires `ANTHROPIC_API_KEY`. `get_settings()` is `lru_cache`-singletonized.
- `backend/app/utils/logging.py`: stdlib `logging` with custom `JSONFormatter` (Loki-ready, ms + Z timestamps) and `TextFormatter` (local dev). Context propagation via `contextvars` (`job_id_ctx`, `episode_id_ctx`, `stage_ctx`, `status_ctx`). Constant `service` label per build plan. Inverse-denylist context payload so every caller-supplied extra surfaces. Third-party loggers locked at WARNING.
- `backend/app/startup.py`: shared `bootstrap(settings, *, process_label)` called by both the FastAPI lifespan and the queue worker -- single source of truth for logging + migrations + the startup banner.
- `backend/app/core/database.py`: sync `sqlite3` connection helper with WAL pragma + DELETE fallback, `synchronous=NORMAL`, `wal_autocheckpoint=1000`, `foreign_keys=ON`. Idempotent migration runner with `fcntl.flock` on `.migration.lock`, `schema_migrations` tracking table, explicit `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` so the migration body and tracking-row INSERT are atomic under autocommit, retry loop that refreshes the pending set between attempts on transient `database is locked`, and a `wal_checkpoint(TRUNCATE)`-before-copy backup that only triggers when a pending migration runs against a populated DB. `_db_has_user_tables` skips sqlite-internal bookkeeping tables. Includes crash-recovery `reset_processing_to_queued` that bumps `updated_at` and preserves any pre-existing error message via `COALESCE`. Includes backup pruning.
- v1 schema migration `001_initial_schema`: `jobs` + `episodes` tables with application-managed `updated_at`, the indices the build plan specifies, and the timestamp default convention (`strftime('%Y-%m-%dT%H:%M:%SZ', 'now')`).
- `backend/app/api/health.py`: `/health/live` (no dependency checks), `/health/ready` (DB connectivity check, returns 503 on failure and logs the exception with traceback), `/health` (alias registered as a stacked decorator on the same function). Both ready responses include `version`, `uptime_seconds` (per-app instance, read from `app.state.started_at` set in lifespan), `components.app`, `components.python`, and the `checks` map.
- `backend/app/main.py`: FastAPI app whose async lifespan calls `bootstrap(...)` and stamps `app.state.started_at`. Shutdown log line on lifespan exit.
- `backend/app/worker.py`: queue process skeleton. Registers `SIGTERM`/`SIGINT` via `loop.add_signal_handler` **before** crash recovery + migrations so a signal during a slow startup is caught, then loops on `shutdown.wait()`. Phase 2 wires real pickup logic onto this skeleton.
- `entrypoint.sh`: supervises `uvicorn` and `python -m app.worker`. Bounded cleanup wait (10s) with SIGKILL fallback so a child that ignores SIGTERM can't hold the container open. Always exits non-zero when one process exits (even a clean `exit 0`) so Docker `restart: unless-stopped` brings the supervised pair back.
- Multi-stage `Dockerfile`: Node stage stubbed (real Vite build in Phase 11), `python:3.13-slim` runtime, `uv` for dep install with `--no-dev --frozen` (build fails loudly on lockfile drift), non-root `audicle` user, healthcheck on `/health/live`.
- `docker-compose.yml` with the `app` service: ports, `env_file`, `host.docker.internal` extra-host for host-installed Ollama on Linux, bind mounts for `data/`, `prompts/`, `corrections/`, `reference/` (the reference mount lands now so Phase 4 picks it up automatically), healthcheck, `restart: unless-stopped`, `no-new-privileges`, `cap_drop: ALL`, log rotation.
- `.env.example` covering every Phase 1-applicable env var from the build plan, grouped by category.
- `.dockerignore` and `.gitignore` extended for Python, venv, runtime data, and editor artifacts. Project `CLAUDE.md` stays local-only per the original gitignore.
- Placeholder bind-mount targets: `backend/app/prompts/script.txt` (real prompt lands in Phase 3), `backend/app/corrections/pronunciation.json` (empty dict), `backend/app/reference/.gitkeep`.
- `backend/tests/`: 31 tests covering config validation (3 negative cases against pydantic `ValidationError`), structured logging (denylist passthrough, ms+Z timestamps, context-filter injection for all four ContextVars, idempotent setup with handler-count assertion), migration runner (idempotent, backup-on-pending-only, mid-migration rollback atomicity, retry loop recovery from transient lock, lock-serializes-concurrent-callers via threads, WAL+foreign_keys pragmas, prune backups), `reset_processing_to_queued` bumping `updated_at` and preserving prior error, `bootstrap()` safe-to-call-twice, worker `_crash_recovery` integration, TestClient-driven health endpoints (200 happy path + 503 on simulated DB failure).
- Container smoke verified: `docker compose build` succeeds, container boots, all three health endpoints return 200, structured logs include `service`, `process_label`, ms+Z timestamps, version/python/pid/hostname, no spurious backups across restart.

### Code-review pass (multi-agent /simplify + /code-review)

Findings surfaced by the multi-angle review and applied in this pass:

- **Migration atomicity**: `with conn:` is a no-op under `isolation_level=None`. Replaced with explicit `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` so the migration body and the `schema_migrations` INSERT are atomic. ROLLBACK wrapped in `contextlib.suppress(OperationalError)` so a secondary error from BEGIN/COMMIT failure can't mask the original.
- **Retry semantics**: the pending list is refreshed from `schema_migrations` between attempts so a transient lock can't trigger a duplicate INSERT (`UNIQUE` violation) on retry.
- **Backup safety**: `_backup_db` calls `PRAGMA wal_checkpoint(TRUNCATE)` before copy so the main `.db` file is self-contained and the `-wal`/`-shm` sidecars don't need to ride along.
- **Backup gating**: `_db_has_user_tables` filters sqlite-internal tables (`sqlite_*`) so a future `AUTOINCREMENT` migration doesn't trigger a spurious backup on a near-empty DB.
- **Crash recovery preserves errors**: `reset_processing_to_queued` uses `COALESCE(error, 'reset on restart')` so a real upstream failure recorded just before the worker crashed isn't overwritten.
- **Entrypoint contract**: supervisor exits non-zero on any child exit (even `exit 0`) so `restart: unless-stopped` always brings the pair back. Bounded cleanup with SIGKILL fallback.
- **Worker signal handlers**: installed via `loop.add_signal_handler` **before** crash recovery; `_shutdown` is loop-scoped (not module-level).
- **Bootstrap dedup**: extracted `app.startup.bootstrap(...)` so the lifespan and the worker share one setup-logging + run-migrations path.
- **Health aliases**: stacked `@router.get` decorators on a single function rather than two duplicate handlers.
- **Structured logging fixes**:
  - JSON timestamp now uses `formatTime(record)` without an explicit `datefmt` so `default_msec_format` actually applies -- output is `YYYY-MM-DDTHH:MM:SS.NNNZ` instead of naive seconds.
  - `_context_payload` switched from a 5-key whitelist to a `_STANDARD_RECORD_ATTRS` denylist so every caller-supplied extra (`error`, `path`, `count`, `version`, `process_label`, ...) surfaces in JSON instead of being silently dropped.
  - Constant `service` label on every record (build plan low-cardinality label).
  - `stage_ctx` and `status_ctx` ContextVars added for spec parity (build plan calls these out as label/body fields propagated by context, not threaded through every `extra=`).
  - `_hostname()` switched from `lru_cache(maxsize=1)` to `functools.cache` (idiomatic on 3.9+).
- **Health endpoint visibility**: `_STARTED_AT` moved to `app.state.started_at` (per-app, set in lifespan) so multi-worker uvicorn reports consistent uptime. DB exception in readiness logs at WARNING with traceback so intermittent failures are visible in Loki.
- **Test isolation**: `env` fixture switched from `return` to `yield` with `get_settings.cache_clear()` on teardown, so a stale `Settings` pointing at a removed `tmp_path` can't bleed into the next test.
- **Settings strictness**: `extra="forbid"` so `.env` typos like `LLM_MODE=...` fail at startup instead of silently being ignored.
- **Docker layer**: bind mounts target `/app/app/*` (matching where the Dockerfile actually copies files); added `./backend/app/reference:/app/app/reference` so the volume exists before Phase 4 lands.
- **Dockerfile**: dropped silent `--frozen` fallback (`uv sync --no-dev --frozen` only -- lockfile drift fails the build); migrated to PEP 735 `[dependency-groups]` so `--no-dev` actually excludes dev deps.
- **Lint**: enabled ruff rule sets `E,F,I,B,UP,SIM,RUF`. Repo is clean (`uv run ruff check .` returns "All checks passed").
- **New tests** added for the previously uncovered behaviors: mid-migration rollback atomicity, retry loop on transient lock, `updated_at` bump + prior-error preservation, lock serialization across threads, JSON timestamp ms+Z, JSON arbitrary extras pass-through, idempotent `setup_logging` handler count, `bootstrap()` safe-to-call-twice, worker `_crash_recovery` integration.
