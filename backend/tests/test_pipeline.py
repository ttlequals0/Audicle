from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.config import get_settings
from app.core import database
from app.services import extraction, jobs, llm, pipeline


def _stub_full_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub extract + llm.generate so Phase 3 pipeline tests don't reach the
    network. Cleanup output is long enough to clear MIN_CLEANUP_CHARS."""

    async def _fake_extract(_url, _settings):
        return extraction.ExtractionResult(markdown="raw " * 250, metadata={"title": "Example"})

    async def _fake_llm(_system, _user, _settings, **_kwargs):
        return "cleaned narration text " * 50

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    monkeypatch.setattr(llm, "generate", _fake_llm)


def _seed_job(env: Path, url: str = "https://example.test/article") -> jobs.Job:
    conn = database.connect(database.db_path(env))
    try:
        jobs.create_job(conn, url)
        # Move it to processing so process_job sees the same state the worker would.
        claimed = jobs.claim_next_queued(conn)
        assert claimed is not None
        return claimed
    finally:
        conn.close()


def _job_after(env: Path, job_id: str) -> jobs.Job:
    conn = database.connect(database.db_path(env))
    try:
        job = jobs.get_job(conn, job_id)
    finally:
        conn.close()
    assert job is not None
    return job


async def test_pipeline_marks_job_done_on_success(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)
    _stub_full_chain(monkeypatch)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "done"
    assert after.stage == "corrections"
    assert after.error is None


async def test_pipeline_marks_failed_with_stage_and_error_on_extraction_failure(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)

    async def _bad_extract(url, settings):
        raise extraction.ExtractionPermanentError("Firecrawl said no")

    monkeypatch.setattr(extraction, "extract", _bad_extract)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "failed"
    assert after.stage == "extract"
    assert after.error is not None
    assert "Firecrawl said no" in after.error


async def test_pipeline_marks_failed_with_timeout_error_on_job_timeout(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)

    async def _slow_extract(url, settings):
        await asyncio.sleep(2)
        return extraction.ExtractionResult(markdown="x" * 1000, metadata={})

    monkeypatch.setattr(extraction, "extract", _slow_extract)
    monkeypatch.setenv("JOB_TIMEOUT_SECONDS", "0.05")
    get_settings.cache_clear()

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "failed"
    assert after.stage == "extract"
    assert "JOB_TIMEOUT_SECONDS" in (after.error or "")


async def test_pipeline_marks_failed_when_cleanup_returns_too_short(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MIN_CLEANUP_CHARS guard fires when the LLM returns near-empty output.
    Stage must be recorded as 'cleanup' and error must reference the limit."""

    database.run_migrations(env)

    async def _fake_extract(_url, _settings):
        return extraction.ExtractionResult(markdown="real article body " * 200, metadata={})

    async def _short_llm(_system, _user, _settings, **_kwargs):
        return "too short"  # below MIN_CLEANUP_CHARS=200

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    monkeypatch.setattr(llm, "generate", _short_llm)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "failed"
    assert after.stage == "cleanup"
    assert "MIN_CLEANUP_CHARS" in (after.error or "")


async def test_pipeline_retries_cleanup_on_transient_llm_provider_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM_RETRY_COUNT controls how many attempts the cleanup stage makes
    before giving up. A single transient LLMProviderError must NOT fail the job."""

    database.run_migrations(env)

    async def _fake_extract(_url, _settings):
        return extraction.ExtractionResult(markdown="x " * 500, metadata={})

    attempts = {"n": 0}

    async def _flaky_llm(_system, _user, _settings, **_kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise llm.LLMProviderError("transient 502")
        return "cleaned narration text " * 50

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    monkeypatch.setattr(llm, "generate", _flaky_llm)

    # Speed the tenacity backoff.
    import tenacity

    monkeypatch.setattr(tenacity.wait_exponential, "__call__", lambda self, rs: 0)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "done"
    assert after.stage == "corrections"
    assert attempts["n"] >= 2  # retried at least once


async def test_pipeline_does_not_retry_on_llm_request_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4xx errors from the LLM (LLMRequestError) must NOT be retried -- the
    request is permanently malformed and retrying wastes time."""

    database.run_migrations(env)

    async def _fake_extract(_url, _settings):
        return extraction.ExtractionResult(markdown="x " * 500, metadata={})

    attempts = {"n": 0}

    async def _always_400(_system, _user, _settings, **_kwargs):
        attempts["n"] += 1
        raise llm.LLMRequestError("400 from provider")

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    monkeypatch.setattr(llm, "generate", _always_400)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "failed"
    assert after.stage == "cleanup"
    assert attempts["n"] == 1  # exactly one attempt
