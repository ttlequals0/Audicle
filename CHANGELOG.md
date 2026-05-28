# Changelog

All notable changes to Audicle are recorded here. Format follows Keep a Changelog
(https://keepachangelog.com). Versioning is semver once a release ships; pre-release
work lives under `[Unreleased]`.

## [Unreleased]

### Added (Phase 2 - Extraction)

- Runtime deps: `httpx>=0.27`, `tenacity>=9.0`. `httpx` moved out of `[dependency-groups].dev` into runtime since the Firecrawl client and the reachability prober both use it.
- `backend/app/services/extraction.py`: async Firecrawl client. POST `{FIRECRAWL_URL}/v1/scrape` with `{url, formats: ["markdown"]}`. Tenacity `AsyncRetrying` with `FIRECRAWL_RETRY_COUNT` attempts and exponential backoff seeded by `FIRECRAWL_BACKOFF_BASE_SECONDS`. Typed exceptions: `ExtractionError` (base), `ExtractionTransientError` (5xx / network / timeout, retryable), `ExtractionPermanentError` (4xx / malformed JSON / `success=false`), `ExtractionTooShortError` (below `MIN_EXTRACTION_CHARS`). Returns a frozen `ExtractionResult(markdown, metadata)`.
- `backend/app/services/jobs.py`: pure DB helpers. `compute_episode_id(url)` = MD5 truncated to 12 hex; `get_job`, `get_job_by_episode_id`, `episode_exists`, `create_job` (with `DuplicateSubmissionError` and reprocess-wipes-prior semantics), `claim_next_queued` (atomic SELECT+UPDATE under `BEGIN IMMEDIATE`), `set_stage`, `mark_done`, `mark_failed`, `job_as_dict`. Every UPDATE bumps `updated_at` explicitly per the build plan's application-managed timestamps contract.
- `backend/app/services/pipeline.py`: `process_job(job, settings)` orchestrator. Wraps the whole job under `asyncio.wait_for(JOB_TIMEOUT_SECONDS)`. Each stage writes its name to `jobs.stage` BEFORE running so the timeout path can report which stage was executing. Stage start/end/failure structured logs with `job_id` + `episode_id` + `stage` stamped via contextvars. Phase 2 wires only the `extract` stage; on success status=done with `stage=extract`. Phase 3+ will append cleanup and beyond.
- `backend/app/services/reachability.py`: `check_firecrawl(settings)` and `run_all(settings)`. Probes `GET {FIRECRAWL_URL}/v1/health`. The worker calls `run_all` at startup and exits non-zero on failure so the container restart loop surfaces a misconfigured stack instead of every job failing the same way mid-pipeline. The FastAPI lifespan deliberately does NOT call `run_all` so `/health/ready` and the admin API stay reachable for triage even when Firecrawl is down.
- `backend/app/worker.py`: polling loop now actually picks up queued jobs (`_pickup_once`), runs `pipeline.process_job`, and continues. Single in-flight. Reachability checks run before crash recovery and signal-handler install; failure causes `sys.exit(1)` so the supervisor cycles the container.
- `backend/app/api/errors.py`: global error envelope. All 4xx and 5xx return `{error, status, details?}` per build plan. RequestValidationError → 400 with field details. Unhandled exceptions → 500 with logged traceback but no client-side leakage.
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
- `test_api_v1.py`: 5 new tests — `extra="forbid"` rejects typo'd field, raw URL preserved through to status, reprocess+inflight still returns 409 with the right reason, status endpoint returns failed jobs with stage + error, 500 envelope contract (no detail leakage, never logs exc.args into response).

- New tests (17 new, 59 total Phase 2 starting point pre-review):
  - `test_extraction.py`: happy path, retries-then-succeeds on 5xx, no-retry on 4xx, retry exhaustion on persistent 5xx, MIN_EXTRACTION_CHARS guard, `success=false` rejection. Uses `httpx.MockTransport` via a factory monkeypatch on `httpx.AsyncClient`.
  - `test_pipeline.py`: status=done with stage=extract on success; status=failed with stage+error on extraction error; status=failed with `JOB_TIMEOUT_SECONDS` error on timeout (last persisted stage reported).
  - `test_api_v1.py`: submit 201 + 12-char episode_id; submit 400 on invalid URL via the validation handler; submit 409 on in-flight duplicate; submit reprocess=true wipes prior episode and returns `replaced_previous=true`; status 200 with full envelope; status 404 with envelope.
  - `test_worker.py`: added `test_pickup_runs_pipeline_against_a_queued_job` (end-to-end with stubbed extractor) and `test_pickup_returns_false_when_no_queued_jobs`; existing `_crash_recovery` + run-loop tests still pass.
- Container smoke verified end-to-end with a mock Firecrawl on `:13002`: reachability check passes, `POST /api/v1/submit` returns 201, status progresses queued → done within seconds, structured logs include `pipeline_start`, `stage_start`, `extract_complete` (markdown_chars=1900, has_title=true), `stage_end` (duration_ms=7), `pipeline_done`. Contextvars correctly stamp `job_id` + `episode_id` + `stage` on every record.

### Added (Phase 1 - Project Scaffold)

- Repo layout per the build plan: `backend/app/{api,core,utils,prompts,corrections,reference}`, `backend/tests/`, `data/`.
- `pyproject.toml` (uv-managed, Python 3.13). Runtime deps: `fastapi`, `uvicorn[standard]`, `pydantic`, `pydantic-settings`. Dev deps live under PEP 735 `[dependency-groups].dev` so `uv sync --no-dev` excludes them in the image. Marked as a uv virtual project (no wheel build).
- `backend/app/version.py` as the single source of `__version__`.
- `backend/app/config.py`: Pydantic `BaseSettings` covering the required, conditional, and tunable env vars from the build plan. `extra="forbid"` so typo'd keys in `.env` fail loudly. Provider-specific validation: `openai-compatible` requires `OPENAI_BASE_URL` + `OPENAI_API_KEY`; `anthropic` requires `ANTHROPIC_API_KEY`. `get_settings()` is `lru_cache`-singletonized.
- `backend/app/utils/logging.py`: stdlib `logging` with custom `JSONFormatter` (Loki-ready, ms + Z timestamps) and `TextFormatter` (local dev). Context propagation via `contextvars` (`job_id_ctx`, `episode_id_ctx`, `stage_ctx`, `status_ctx`). Constant `service` label per build plan. Inverse-denylist context payload so every caller-supplied extra surfaces. Third-party loggers locked at WARNING.
- `backend/app/startup.py`: shared `bootstrap(settings, *, process_label)` called by both the FastAPI lifespan and the queue worker — single source of truth for logging + migrations + the startup banner.
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
  - JSON timestamp now uses `formatTime(record)` without an explicit `datefmt` so `default_msec_format` actually applies — output is `YYYY-MM-DDTHH:MM:SS.NNNZ` instead of naive seconds.
  - `_context_payload` switched from a 5-key whitelist to a `_STANDARD_RECORD_ATTRS` denylist so every caller-supplied extra (`error`, `path`, `count`, `version`, `process_label`, ...) surfaces in JSON instead of being silently dropped.
  - Constant `service` label on every record (build plan low-cardinality label).
  - `stage_ctx` and `status_ctx` ContextVars added for spec parity (build plan calls these out as label/body fields propagated by context, not threaded through every `extra=`).
  - `_hostname()` switched from `lru_cache(maxsize=1)` to `functools.cache` (idiomatic on 3.9+).
- **Health endpoint visibility**: `_STARTED_AT` moved to `app.state.started_at` (per-app, set in lifespan) so multi-worker uvicorn reports consistent uptime. DB exception in readiness logs at WARNING with traceback so intermittent failures are visible in Loki.
- **Test isolation**: `env` fixture switched from `return` to `yield` with `get_settings.cache_clear()` on teardown, so a stale `Settings` pointing at a removed `tmp_path` can't bleed into the next test.
- **Settings strictness**: `extra="forbid"` so `.env` typos like `LLM_MODE=...` fail at startup instead of silently being ignored.
- **Docker layer**: bind mounts target `/app/app/*` (matching where the Dockerfile actually copies files); added `./backend/app/reference:/app/app/reference` so the volume exists before Phase 4 lands.
- **Dockerfile**: dropped silent `--frozen` fallback (`uv sync --no-dev --frozen` only — lockfile drift fails the build); migrated to PEP 735 `[dependency-groups]` so `--no-dev` actually excludes dev deps.
- **Lint**: enabled ruff rule sets `E,F,I,B,UP,SIM,RUF`. Repo is clean (`uv run ruff check .` returns "All checks passed").
- **New tests** added for the previously uncovered behaviors: mid-migration rollback atomicity, retry loop on transient lock, `updated_at` bump + prior-error preservation, lock serialization across threads, JSON timestamp ms+Z, JSON arbitrary extras pass-through, idempotent `setup_logging` handler count, `bootstrap()` safe-to-call-twice, worker `_crash_recovery` integration.
