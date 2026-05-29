"""Queue worker process.

Polls the jobs table for ``status='queued'`` rows. Single in-flight job: the
pipeline runs sequentially and the next poll only happens after the previous
job finishes (or fails). Crash recovery and reachability checks gate the
polling loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from datetime import UTC, datetime

from app.config import Settings, get_settings
from app.core import database
from app.services import jobs, pipeline, reachability, retention, runtime_settings
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
        retention.purge_older_than(overlaid, older_than_days=overlaid.RETENTION_DAYS)
        retention.purge_expired_jobs(overlaid, older_than_days=overlaid.RETENTION_DAYS)
        retention.sweep_orphan_media(overlaid)
        database.prune_backups(
            overlaid.DATA_DIR, retention_days=overlaid.MIGRATION_BACKUP_RETENTION_DAYS
        )
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
    await pipeline.process_job(job, settings)
    return True


async def run() -> None:
    settings = get_settings()
    bootstrap(settings, process_label="worker")

    try:
        await reachability.run_all(settings)
    except reachability.ReachabilityError as exc:
        logger.error(
            "Startup reachability checks failed; exiting",
            extra={"event": "reachability_fatal", "stage": "startup", "error": str(exc)},
        )
        sys.exit(1)

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
            # Loop right back: a job may have arrived while we were working.
            continue
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=poll_interval)

    logger.info("Worker stopped", extra={"event": "worker_stopped"})


if __name__ == "__main__":
    asyncio.run(run())
