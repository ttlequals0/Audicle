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
    call _process_one, observe the job reach status=done with stage=transcript."""

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
        return (
            "Structured cleaned narration sentence. "
            "Another sentence with end-of-sentence punctuation. "
            "More sentences keep the chunker happy. "
        ) * 30

    from app.services import audio, llm, tts

    async def _fake_tts(text, episode_id, chunk_index, settings, pronunciations=None):
        _ = text  # acknowledge
        _ = settings
        return tts.GenerateResult(
            wav_path=f"/tmp/{episode_id}_chunk_{chunk_index}.wav",
            duration_secs=1.0,
            sample_rate=24000,
        )

    def _fake_concat(_paths, output_path, _settings):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"FAKE_WAV")
        return output_path, 24000

    def _fake_encode(_input_wav, output_mp3, _settings):
        output_mp3.parent.mkdir(parents=True, exist_ok=True)
        output_mp3.write_bytes(b"FAKE_MP3")
        return audio.EncodeResult(mp3_path=output_mp3, duration_secs=2.5)

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    monkeypatch.setattr(llm, "generate", _fake_llm)
    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)
    monkeypatch.setattr(audio, "concat_with_padding", _fake_concat)
    monkeypatch.setattr(audio, "normalize_and_encode", _fake_encode)

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
    assert row["stage"] == "finalize"
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


async def test_run_does_not_exit_when_reachability_fails(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reachability is advisory: a down dependency must not raise SystemExit
    or otherwise stop the worker from entering its poll loop."""

    from app import worker
    from app.services import reachability

    async def _stub_run_all(_settings):
        return [reachability.CheckResult(name="firecrawl", ok=False, detail="down")]

    monkeypatch.setattr(reachability, "run_all", _stub_run_all)

    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.2)
    # The loop is still running (no SystemExit / early return).
    assert not task.done()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
