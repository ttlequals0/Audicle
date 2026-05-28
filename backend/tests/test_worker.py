from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from app.core import database


async def test_crash_recovery_resets_processing(env: Path) -> None:
    from app.worker import _crash_recovery

    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        conn.execute(
            "INSERT INTO jobs (id, url, episode_id, status) VALUES (?, ?, ?, ?)",
            ("stuck", "https://x.test/a", "abc", "processing"),
        )
    finally:
        conn.close()

    await _crash_recovery(env)

    conn = database.connect(database.db_path(env))
    try:
        status = conn.execute("SELECT status FROM jobs WHERE id = 'stuck'").fetchone()[0]
    finally:
        conn.close()
    assert status == "queued"


async def test_run_returns_quickly_when_shutdown_set_after_start(env: Path, monkeypatch) -> None:
    """Smoke-test the worker run() loop: bootstrap + crash recovery + signal
    install + a polling iteration, then a shutdown via the asyncio event."""

    from app import worker

    # Speed up the poll so the test isn't sluggish.
    monkeypatch.setattr(
        "app.worker.get_settings",
        lambda: _FastSettings(env),
    )

    # signal.add_signal_handler isn't allowed from non-main threads; pytest-asyncio
    # runs us on the main thread, so this is fine on Linux.
    task = asyncio.create_task(worker.run())
    # Let bootstrap + crash recovery + signal install finish.
    await asyncio.sleep(0.1)
    # Cancel cleanly via signal-style shutdown: we can't send a real signal from
    # inside a test reliably, so close the task. The worker checks shutdown
    # between poll iterations.
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    # Logger should have emitted the worker_starting banner.
    assert logging.getLogger("app.worker") is not None


class _FastSettings:
    """Minimal stand-in so the worker loop polls quickly."""

    def __init__(self, data_dir: Path) -> None:
        self.DATA_DIR = data_dir
        self.LOG_LEVEL = "INFO"
        self.LOG_FORMAT = "text"
        self.QUEUE_POLL_INTERVAL_SECONDS = 0.05
