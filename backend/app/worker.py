"""Queue worker process.

Polls the jobs table for ``status='queued'`` rows. Single in-flight job: the
pipeline runs sequentially and the next poll only happens after the previous
job finishes (or fails). Crash recovery runs before the loop; reachability is
advisory (logged, never blocks).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime

from app.config import RUNTIME_SETTING_BOUNDS, Settings, get_settings
from app.core import database
from app.services import (
    jobs,
    pipeline,
    reachability,
    retention,
    runtime_settings,
    tts,
    tts_cache,
)
from app.startup import bootstrap

logger = logging.getLogger("app.worker")


async def _crash_recovery(data_dir) -> None:
    conn = database.connect(database.db_path(data_dir))
    try:
        reset = database.reset_processing_to_queued(conn)
        if reset:
            logger.info(
                "Reset stuck processing jobs",
                extra={"event": "crash_recovery", "reset": reset, "stage": "startup"},
            )
    finally:
        conn.close()


async def _pickup_once(settings: Settings) -> jobs.Job | None:
    """Claim the oldest queued job, if any."""

    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        return jobs.claim_next_queued(conn)
    finally:
        conn.close()


def _maybe_run_retention_sweep(settings: Settings, last_sweep_day: str | None) -> str | None:
    """Run the retention sweep at most once per UTC day, at the configured
    hour. Returns the new value of ``last_sweep_day`` so the caller can
    persist the de-dup state across iterations.

    Stateless across restarts (re-runs if the worker bounces past the sweep
    hour without having run today); the operation is idempotent so a double
    sweep just emits two `retention_sweep_complete` log lines.
    """

    now = datetime.now(UTC)
    today = now.strftime("%Y-%m-%d")
    if now.hour != settings.RETENTION_SWEEP_HOUR_UTC:
        return last_sweep_day
    if last_sweep_day == today:
        return last_sweep_day
    try:
        # Apply DB overrides so RETENTION_DAYS set via PUT /api/v1/settings
        # takes effect on the next sweep without a worker restart.
        overlaid = runtime_settings.overlay(settings)
        # RETENTION_DAYS <= 0 means "keep forever": skip the automatic episode/job
        # purge. The wipe-all-on-zero contract (year-9999 cutoff) is reserved for
        # the explicit operator-initiated POST /api/v1/purge?older_than_days=0
        # &confirm=true, so the env knob can't silently delete everything daily.
        if overlaid.RETENTION_DAYS > 0:
            retention.purge_older_than(overlaid, older_than_days=overlaid.RETENTION_DAYS)
            retention.purge_expired_jobs(overlaid, older_than_days=overlaid.RETENTION_DAYS)
        retention.sweep_orphan_media(overlaid)
        database.prune_backups(
            overlaid.DATA_DIR, retention_days=overlaid.MIGRATION_BACKUP_RETENTION_DAYS
        )
        # TTS_CACHE_RETENTION_DAYS carries its bounds only in
        # RUNTIME_SETTING_BOUNDS (no Field constraint, so a stale env value
        # can't fail startup); clamp here so a misconfigured <=0 value can't
        # turn every sweep into "purge the whole tts_cache".
        cache_bounds = RUNTIME_SETTING_BOUNDS["TTS_CACHE_RETENTION_DAYS"]
        retention_days = min(
            cache_bounds["le"], max(cache_bounds["ge"], overlaid.TTS_CACHE_RETENTION_DAYS)
        )
        tts_cache.purge_older_than(overlaid.DATA_DIR, int(retention_days))
    except Exception:
        logger.exception(
            "Retention sweep failed; will retry next iteration",
            extra={"event": "retention_sweep_failed"},
        )
        return last_sweep_day
    return today


async def _process_one(settings: Settings) -> bool:
    """Pick up and run one job. Returns True if a job was processed."""

    job = await _pickup_once(settings)
    if job is None:
        return False
    # Apply runtime_settings DB overrides per job so Settings-UI edits
    # (LLM/feed/chunk/cleanup tunables) take effect on the next submission
    # without a worker restart -- the resolution chain the build plan promises.
    # A transient overlay read failure must not fail an otherwise-runnable job;
    # fall back to the env settings in that case.
    try:
        effective = runtime_settings.overlay(settings)
    except Exception:
        logger.exception(
            "runtime_settings overlay failed; running job on env settings",
            extra={"event": "overlay_failed", "job_id": job.id},
        )
        effective = settings
    await pipeline.process_job(job, effective)
    return True


async def _maybe_restart_idle_wrapper(settings: Settings) -> None:
    """Opportunistically restart a memory-heavy wrapper between jobs, instead
    of waiting for its own mid-job hard-limit restart. Runs only when the
    queue is empty so it can never delay a job that's waiting to run.

    This is best-effort maintenance, not part of a job's success path: every
    failure (wrapper unreachable, DB hiccup, lock still held so the wrapper
    503s) is swallowed and logged at debug rather than raised, so it can never
    affect the worker loop.
    """

    # Same overlay-with-fallback as _process_one: TTS_IDLE_RESTART_ENABLED,
    # TTS_BACKEND, TTS_URL, and TTS_HTTP_TIMEOUT_SECONDS are all in ALLOWED_KEYS
    # so an operator can flip them from the Settings UI mid-incident; reading
    # the frozen env `settings` here would make that a dead knob until the
    # worker restarts. A transient overlay read failure falls back to env
    # settings rather than skipping the check entirely.
    try:
        effective = runtime_settings.overlay(settings)
    except Exception:
        logger.debug(
            "runtime_settings overlay failed; checking idle restart on env settings",
            extra={"event": "tts_idle_restart_overlay_failed"},
            exc_info=True,
        )
        effective = settings

    if not effective.TTS_IDLE_RESTART_ENABLED or effective.TTS_BACKEND == "openai-api":
        return
    try:
        conn = database.connect(database.db_path(effective.DATA_DIR))
        try:
            queued = jobs.count_queued(conn)
        finally:
            conn.close()
        if queued > 0:
            return
        # Uses TTS_HTTP_TIMEOUT_SECONDS with no retry (unlike the per-chunk
        # calls), so a wrapper that accepts the connection but never answers
        # can delay the next job's pickup by up to ~2x that timeout (health
        # GET + restart POST). Accepted as bounded rather than adding a retry
        # budget to an opportunistic, best-effort check.
        health = await tts.wrapper_health(effective)
        if not health.get("restart_recommended"):
            return
        await tts.request_wrapper_restart(effective)
        logger.info(
            "Idle wrapper restart requested",
            extra={"event": "tts_idle_restart_requested", "rss_mb": health.get("rss_mb")},
        )
    except Exception:
        logger.debug(
            "Idle wrapper restart check failed; skipping",
            extra={"event": "tts_idle_restart_check_failed"},
            exc_info=True,
        )


async def run() -> None:
    settings = get_settings()
    bootstrap(settings, process_label="worker")

    # Advisory only: logs dependency status and a warning if any are down, but
    # never blocks the worker from starting. Operators configure URLs/keys at
    # runtime via the UI; a job that needs a down dependency fails that stage.
    await reachability.run_all(settings)

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown(signum: int) -> None:
        logger.info(
            "Worker shutdown signal",
            extra={"event": "worker_signal", "signum": signum},
        )
        shutdown.set()

    # Install signal handlers BEFORE crash recovery so a SIGTERM during a slow
    # migration or recovery isn't dispatched to the default handler.
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_shutdown, sig)

    await _crash_recovery(settings.DATA_DIR)

    poll_interval = settings.QUEUE_POLL_INTERVAL_SECONDS
    last_sweep_day: str | None = None
    while not shutdown.is_set():
        # The sweep is sync (SQLite + file unlinks); run it in a worker thread
        # so a large purge doesn't block signal handling or the
        # ``shutdown.wait()`` that lets SIGTERM exit cleanly.
        last_sweep_day = await asyncio.to_thread(
            _maybe_run_retention_sweep, settings, last_sweep_day
        )
        # Anything that escapes process_job (DB locked during pickup, OSError
        # on the data dir, ...) must not kill the worker. Log it and back off
        # one poll interval so we don't spin against a hard failure.
        try:
            processed = await _process_one(settings)
        except Exception:
            logger.exception(
                "Worker iteration failed; backing off",
                extra={"event": "worker_iteration_failed"},
            )
            processed = False
        if processed:
            await _maybe_restart_idle_wrapper(settings)
            # Loop right back: a job may have arrived while we were working.
            continue
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=poll_interval)

    logger.info("Worker stopped", extra={"event": "worker_stopped"})


if __name__ == "__main__":
    asyncio.run(run())
