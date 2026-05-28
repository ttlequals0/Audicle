# Changelog

All notable changes to Audicle are recorded here. Format follows Keep a Changelog
(https://keepachangelog.com). Versioning is semver once a release ships; pre-release
work lives under `[Unreleased]`.

## [Unreleased]

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
