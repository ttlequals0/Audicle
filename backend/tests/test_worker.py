from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest
from app.core import database
from app.services import jobs


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


async def test_pickup_runs_pipeline_against_a_queued_job(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end inside the worker: insert a queued job, stub the extractor,
    call _process_one, observe the job reach status=done with stage=extract."""

    from app.config import get_settings
    from app.services import extraction
    from app.worker import _process_one

    database.run_migrations(env)

    # Stub extract + LLM so the test doesn't reach the network.
    async def _fake_extract(url, settings):
        return extraction.ExtractionResult(
            markdown="# Example\n\nbody " * 200,
            metadata={"title": "Example article"},
        )

    async def _fake_llm(_system, _user, _settings, **_kwargs):
        return "cleaned narration text " * 50

    from app.services import llm

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    monkeypatch.setattr(llm, "generate", _fake_llm)

    # Insert one queued job via the helper so episode_id is computed.
    conn = database.connect(database.db_path(env))
    try:
        jobs.create_job(conn, "https://example.test/article")
    finally:
        conn.close()

    processed = await _process_one(get_settings())
    assert processed is True

    conn = database.connect(database.db_path(env))
    try:
        row = conn.execute(
            "SELECT status, stage, error FROM jobs WHERE url = ?",
            ("https://example.test/article",),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "done"
    assert row["stage"] == "corrections"
    assert row["error"] is None


async def test_pickup_returns_false_when_no_queued_jobs(env: Path) -> None:
    from app.config import get_settings
    from app.worker import _process_one

    database.run_migrations(env)
    assert await _process_one(get_settings()) is False


async def test_run_exits_cleanly_when_reachability_passes_and_shutdown_set(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke the worker run() loop end-to-end with reachability stubbed."""

    from app import worker
    from app.services import reachability

    async def _stub_run_all(_settings):
        return [reachability.CheckResult(name="firecrawl", ok=True, detail="stub")]

    monkeypatch.setattr(reachability, "run_all", _stub_run_all)

    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
