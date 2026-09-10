# Deployment runbook

Operational reference for a running Audicle. Domains below are placeholders; substitute your own.

## Health

- `GET /health/live` is a flat liveness probe: `{"ok": true, "version": "..."}`. The fastest way to confirm which version is actually running.
- `GET /health/ready` checks the database and published-media directory. Podcast delivery stays ready during ingestion-provider outages.
- `GET /health/ingestion` checks the selected LLM, extraction, synthesis, and transcription dependencies. Optional renderer status appears under `components` without failing the check.

The wrapper can take 60 to 99 seconds to load its models. `/health/ingestion` reports it unavailable until its own health endpoint succeeds. Job requests use `TTS_REACHABILITY_GRACE_SECONDS` while waiting for startup.

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

The stack pins `BUILD_VERSION` for all four images. Prepare the compatible data before starting an older version:

1. Restore the Compose file and environment that shipped with the target version. A version before the renderer proxy cannot use the current four-image stack unchanged.
2. Test a compatible database backup with its matching media and reference voices in a separate instance. Include any environment-provided session secret.
3. Stop the current stack. Preserve its database, media, voices, environment, and Compose file as a rollback point.
4. Restore the tested data set, set `BUILD_VERSION`, and start the target stack.
5. Confirm with `curl -s https://your-server/health/live` that `version` matches. Then check `/health/ready` for podcast delivery and `/health/ingestion` for processing dependencies.

Version tags are immutable and stay on Docker Hub, so a rollback needs no rebuilding.

## Disk

- **Before any build**: run `df -h /` and `docker system df`. Do not prune unrelated images or caches unless the output shows that space is needed.
- `docker builder prune -af` reclaims the build cache (about 18 GB after a full build). `docker image prune` reclaims nothing when every local image still carries a named tag; remove superseded version tags with `docker rmi` instead. Keep the deployed version and one rollback candidate.
- The `./data` volume holds the SQLite DB, media, and the model caches (`hf_cache/`, `tts_home/`, about 2 GB). Retention (`RETENTION_DAYS`) bounds media growth.

## Data safety

- SQLite migrations use SQLite's online backup API to write a consistent timestamped snapshot before applying. Snapshots are pruned after `MIGRATION_BACKUP_RETENTION_DAYS`.
- A complete backup also needs the media directory, reference voices, session signing secret, and feed-authentication key. Restore those with the database into a separate test instance, run an integrity check, and verify the schema version and referenced media before replacing a running instance.
- `POST /api/v1/purge?confirm=true&older_than_days=N` deletes episodes and their files. The default `older_than_days=0` wipes everything, so pass a cutoff when you mean one.
- `POST /api/v1/feed/recreate?confirm=true` rotates every GUID: all subscribers re-download everything. It is the blunt fallback; per-episode reprocess already bumps its own GUID.

For a complete operator backup outside migrations, stop the stack first. This prevents publication or retention from changing the database and files between copies. Then use SQLite's backup API and copy the media and reference directories before restarting:

Set `AUDICLE_BACKUP_DIR` to storage outside the checkout, preferably an encrypted off-host mount. The directory contains feed and session credentials.

```bash
umask 077
export AUDICLE_BACKUP_DIR=/secure/off-host-mounted/audicle-backup
mkdir -p "$AUDICLE_BACKUP_DIR"
chmod 700 "$AUDICLE_BACKUP_DIR"
python3 - data/podcast.db "$AUDICLE_BACKUP_DIR/podcast.db" <<'PY'
import sqlite3
import sys

with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True) as source, sqlite3.connect(sys.argv[2]) as destination:
    source.backup(destination)
    assert destination.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
PY
tar -czf "$AUDICLE_BACKUP_DIR/files.tgz" data/media data/reference
install -m 600 .env "$AUDICLE_BACKUP_DIR/environment"
```

Test restoration in a separate data directory. Copy the database snapshot, extract its matching files, and restore the saved environment securely. Open the restored database read-only and check its integrity before starting the target image:

```bash
umask 077
export AUDICLE_BACKUP_DIR=/secure/off-host-mounted/audicle-backup
export AUDICLE_RESTORE_DIR=/secure/audicle-restore
mkdir -p "$AUDICLE_RESTORE_DIR"
cp "$AUDICLE_BACKUP_DIR/podcast.db" "$AUDICLE_RESTORE_DIR/podcast.db"
tar -xzf "$AUDICLE_BACKUP_DIR/files.tgz" -C "$AUDICLE_RESTORE_DIR" --strip-components=1
install -m 600 "$AUDICLE_BACKUP_DIR/environment" "$AUDICLE_RESTORE_DIR/.env"
python3 - "$AUDICLE_RESTORE_DIR/podcast.db" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
print(connection.execute("SELECT name FROM schema_migrations ORDER BY rowid DESC LIMIT 1").fetchone()[0])
connection.close()
PY
```

## Security posture

- Set the admin password (Settings > security) before exposing anything. Until then the app runs in open convenience mode and says so in the UI.
- Login is rate limited (`LOGIN_RATE_LIMIT`) with an IP lockout on repeated failures. Behind a reverse proxy, set `TRUST_PROXY_HEADERS=true` and `TRUSTED_PROXY_HOPS` or every client shares the proxy's IP for lockout purposes; do not enable it without a trusted proxy in front.
- [Authenticated feeds](feeds-and-podcasting.md#authenticated-feeds) gate the public feed and media. The render sidecar and TTS wrapper are internal-only; never expose them.

Publish only `GET` and `HEAD` for the configured RSS path and referenced `/media/` assets. Keep `/`, `/api/v1/`, `/redoc`, health routes, and port 8000 on the private administration origin. Preserve feed-key query parameters and keyed artwork paths at the edge. Protected responses use `private, no-store`; bypass and purge shared caches when rotating a feed key.

The public proxy should use an explicit route allowlist:

```nginx
log_format audicle_public '$request_id $remote_addr $request_method $status $request_time';

upstream audicle_app { server app:8000; }

server {
access_log /var/log/nginx/audicle-public.log audicle_public;
error_log /dev/null crit;
location = /rss/your_configured_slug.xml {
    limit_except GET HEAD { deny all; }
    proxy_cache off;
    proxy_pass http://audicle_app;
}
location ~ "^/media/(default(-[0-9a-f]{64})?\.jpg|[0-9a-f]{12}\.(mp3|vtt)|[0-9a-f]{12}\.chapters\.json|[0-9a-f]{12}(-v([0-9a-f]{32}|[0-9]{1,20}))?(-[0-9a-f]{64})?\.jpg)$" {
    limit_except GET HEAD { deny all; }
    proxy_cache off;
    proxy_pass http://audicle_app;
}
location / { return 404; }
}
```

The example assumes the proxy shares a Docker network with the Compose `app` service. Change the upstream when the proxy runs elsewhere. Its access log omits paths, query strings, and headers for every response, including rejected paths. The public server discards error logs because upstream errors can contain a credential-bearing URI; use the safe access status and request ID for diagnosis. Query strings do not participate in location matching, so the allowlist accepts the `?v=<generation>&key=<feed-key>` URLs emitted for audio, transcripts, and chapters.

Add a separate exact expression for `/media/<12-hex-id>.txt` only when cleaned article text should be public. Test RSS, public and keyed default artwork, old and new episode artwork, byte-range audio, transcripts, and chapters through the real public hostname. Confirm that `/api/v1/`, `/redoc`, health routes, and direct port 8000 access fail from outside the administration network.

If a feed key or session cookie reaches a log, deploy redaction first. Then purge shared caches, rotate the feed key or revoke all sessions, and remove retained log copies under the logging system's retention policy. Previously downloaded podcast files cannot be revoked.

The renderer reaches the internet only through `render-egress`. The renderer container has no direct public or private-network route, and the proxy rejects private, loopback, link-local, shared, multicast, and special-use IPv4 destinations. IPv6 renderer egress is disabled. Keep both sidecars on private container networks.

[< Docs index](README.md)
