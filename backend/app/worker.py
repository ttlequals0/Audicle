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

from app.config import Settings, get_settings
from app.core import database
from app.services import jobs, pipeline, reachability
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
    while not shutdown.is_set():
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
