"""Queue worker process.

Phase 1: polling loop that performs crash recovery at startup, then idles. The
pipeline stages (extract, cleanup, tts, ...) are wired up in later phases.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from app.config import get_settings
from app.core import database
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


async def run() -> None:
    settings = get_settings()
    bootstrap(settings, process_label="worker")

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
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=poll_interval)
        except TimeoutError:
            continue

    logger.info("Worker stopped", extra={"event": "worker_stopped"})


if __name__ == "__main__":
    asyncio.run(run())
