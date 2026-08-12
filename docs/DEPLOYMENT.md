# Deployment runbook

Operational reference for a running Audicle. Domains below are placeholders; substitute your own.

## Health

- `GET /health/live` is a flat liveness probe: `{"ok": true, "version": "..."}`. The fastest way to confirm which version is actually running.
- `GET /health/ready` aggregates DB, TTS wrapper, Firecrawl, and LLM probes and returns 503 if any fails. The ffmpeg version and render-sidecar reachability are reported under `components` but never fail readiness. When something is broken, start here: it names the unhappy dependency.

The wrapper takes 60 to 99 seconds after a (re)start to load its models; the app's readiness probe waits out a grace window (`TTS_REACHABILITY_GRACE_SECONDS`) before calling it down.

## Logs

Structured JSON to stdout (`LOG_FORMAT=json`, the default; `text` for readable local output). `docker compose logs app` / `tts-wrapper` / `render`. `LOG_LEVEL` is runtime-tunable, so a live deployment can go to DEBUG for one job and back without a restart.

Events worth alerting on or graphing:

| Event | Container | Meaning |
|---|---|---|
| `pipeline_failed` / `stage_failed` | app | A job died; `stage` and the exception say where |
| `reachability_degraded` | app | Started with a dependency down; jobs needing it fail that stage until it recovers |
| `chunk_quality_bad` / `chunk_quality_unresolved` | app | The quality gate rejected a take / gave up after max regens and kept the best |
| `tts_chunk_done` (`rss_mb` field) | tts-wrapper | Per-chunk resident memory; the growth curve of a long job |
| `tts_memory_cleanup` | tts-wrapper | The soft limit fired; `reclaimed_mb` says what trimming got back |
| `tts_memory_restart` | tts-wrapper | The hard limit fired; the wrapper restarts at the next chunk boundary. Expected on very long jobs, not an error |
| `whisper_remote_error` | app | Remote ASR failed; the chunk shipped unverified |

## What the common failures mean

- **`TTS unreachable: All connection attempts failed`** - nothing was listening at `TTS_URL` for longer than the connect budget (`TTS_CONNECT_RETRY_MAX_SECONDS`, default 180 s). A wrapper restart is covered by that budget; exceeding it means the wrapper is staying down. Check its logs, then the host's kernel log for an OOM kill.
- **A job failed at `extract`** - the error names the fix: a hard block with no solver points at `FLARESOLVERR_URL`; a solver that could not clear it means the site needs a login; a short teaser means add a [site override](paywalls.md).
- **A job killed by the watchdog** - the error says whether the stall window or the absolute ceiling fired; both are tunable under Job timeouts in Settings.
- **The wrapper restarts periodically during a very long job** - that is the memory ladder's hard limit doing its job at a chunk boundary, and the connect budget rides it out. Only worry if jobs fail, or the kernel log shows the OOM killer rather than a `tts_memory_restart` event.

## Rolling back

The stack pins `BUILD_VERSION` for all three images, so a rollback is redeploying with the previous version:

1. Set the stack's `BUILD_VERSION` to the last good version and redeploy.
2. The app container takes about 30 seconds to come back; the wrapper another minute or two of model loading.
3. Confirm with `curl -s https://your-server/health/live` that `version` matches, then `/health/ready` for the components.

Version tags are immutable and stay on Docker Hub, so a rollback needs no rebuilding.

## Disk

- **Before any build**: `df -h /` and `docker system df`. A three-image build wants roughly 30 GB free and fails at `exporting layers` without it.
- `docker builder prune -af` reclaims the build cache (about 18 GB after a full build). `docker image prune` reclaims nothing when every local image still carries a named tag; remove superseded version tags with `docker rmi` instead. Keep the deployed version and one rollback candidate.
- The `./data` volume holds the SQLite DB, media, and the model caches (`hf_cache/`, `tts_home/`, about 2 GB). Retention (`RETENTION_DAYS`) bounds media growth.

## Data safety

- SQLite migrations write a timestamped backup before applying (pruned after `MIGRATION_BACKUP_RETENTION_DAYS`). Never edit the DB while the stack is up.
- `POST /api/v1/purge?confirm=true&older_than_days=N` deletes episodes and their files. The default `older_than_days=0` wipes everything, so pass a cutoff when you mean one.
- `POST /api/v1/feed/recreate?confirm=true` rotates every GUID: all subscribers re-download everything. It is the blunt fallback; per-episode reprocess already bumps its own GUID.

## Security posture

- Set the admin password (Settings > security) before exposing anything. Until then the app runs in open convenience mode and says so in the UI.
- Login is rate limited (`LOGIN_RATE_LIMIT`) with an IP lockout on repeated failures. Behind a reverse proxy, set `TRUST_PROXY_HEADERS=true` and `TRUSTED_PROXY_HOPS` or every client shares the proxy's IP for lockout purposes; do not enable it without a trusted proxy in front.
- [Authenticated feeds](feeds-and-podcasting.md#authenticated-feeds) gate the public feed and media. The render sidecar and TTS wrapper are internal-only; never expose them.

[< Docs index](README.md)
