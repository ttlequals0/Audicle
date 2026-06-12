from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from app.config import get_settings
from app.core import database
from app.services import extraction, jobs, llm, pipeline, transcript
from PIL import Image


def _stub_full_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub extract + llm.generate so Phase 3 pipeline tests don't reach the
    network. Cleanup output is long enough to clear MIN_CLEANUP_CHARS."""

    async def _fake_extract(_url, _settings, _registry=None):
        return extraction.ExtractionResult(markdown="raw " * 250, metadata={"title": "Example"})

    async def _fake_llm(_system, _user, _settings, **_kwargs):
        # Provide structured prose with sentence boundaries so the chunker
        # has somewhere to split.
        return (
            "This is a cleaned narration sentence with proper punctuation. "
            "Each sentence ends with a period. "
            "Paragraphs are separated by blank lines.\n\n"
            "Here is another paragraph of cleaned narration text. "
            "The chunker can split on these boundaries. "
            "Forty more sentences follow for word-count headroom. "
        ) * 10

    from app.services import audio, tts

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False):
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


def _stub_tts_and_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub only the tts + audio touch points (re-used by tests that compose
    their own llm/extract stubs)."""

    from app.services import audio, tts

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False):
        _ = text
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

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)
    monkeypatch.setattr(audio, "concat_with_padding", _fake_concat)
    monkeypatch.setattr(audio, "normalize_and_encode", _fake_encode)


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
    assert after.stage == "finalize"
    assert after.error is None


async def test_pipeline_marks_failed_with_stage_and_error_on_extraction_failure(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)

    async def _bad_extract(url, settings, registry=None):
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

    async def _slow_extract(url, settings, registry=None):
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


def test_effective_job_timeout_scales_with_chunk_count() -> None:
    s = SimpleNamespace(JOB_TIMEOUT_SECONDS=3600.0, JOB_TIMEOUT_PER_CHUNK_SECONDS=30.0)
    assert pipeline.effective_job_timeout(s, 0) == 3600.0
    assert pipeline.effective_job_timeout(s, 100) == 3600.0  # base floor wins
    assert pipeline.effective_job_timeout(s, 200) == 6000.0  # chunk-scaled wins


async def test_pipeline_reschedules_timeout_for_long_document(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A document with enough chunks gets a rescaled (longer) timeout, so a job
    that would blow the base ceiling during TTS still completes."""

    database.run_migrations(env)
    _stub_full_chain(monkeypatch)

    from app.services import chunker, tts

    monkeypatch.setattr(chunker, "chunk", lambda *a, **k: ["a sentence."] * 10)

    async def _slow_tts(text, episode_id, chunk_index, settings, seed=None, verify=False):
        await asyncio.sleep(0.08)
        return tts.GenerateResult(
            wav_path=f"/tmp/{episode_id}_chunk_{chunk_index}.wav",
            duration_secs=1.0,
            sample_rate=24000,
        )

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _slow_tts)
    # base 0.5s would time out mid-TTS (10 x 0.08 = 0.8s); rescaled to
    # max(0.5, 10 x 0.5 = 5.0) it finishes.
    monkeypatch.setenv("JOB_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("JOB_TIMEOUT_PER_CHUNK_SECONDS", "0.5")
    get_settings.cache_clear()

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "done"
    assert after.error is None


async def test_pipeline_still_times_out_when_scaled_budget_exceeded(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scaled timeout is still a ceiling: a job slower than max(base,
    chunks x per-chunk) fails, and the error reports the chunk count."""

    database.run_migrations(env)
    _stub_full_chain(monkeypatch)

    from app.services import chunker, tts

    monkeypatch.setattr(chunker, "chunk", lambda *a, **k: ["a sentence."] * 5)

    async def _very_slow_tts(text, episode_id, chunk_index, settings, seed=None, verify=False):
        await asyncio.sleep(0.3)
        return tts.GenerateResult(
            wav_path=f"/tmp/{episode_id}_chunk_{chunk_index}.wav",
            duration_secs=1.0,
            sample_rate=24000,
        )

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _very_slow_tts)
    # effective = max(0.3, 5 x 0.1 = 0.5) = 0.5s; TTS needs ~1.5s, so it times out.
    monkeypatch.setenv("JOB_TIMEOUT_SECONDS", "0.3")
    monkeypatch.setenv("JOB_TIMEOUT_PER_CHUNK_SECONDS", "0.1")
    get_settings.cache_clear()

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "failed"
    assert after.stage == "tts"
    assert "5 chunks" in (after.error or "")
    assert "JOB_TIMEOUT_SECONDS" in (after.error or "")


async def test_pipeline_marks_failed_when_cleanup_returns_too_short(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MIN_CLEANUP_CHARS guard fires when the LLM returns near-empty output.
    Stage must be recorded as 'cleanup' and error must reference the limit."""

    database.run_migrations(env)

    async def _fake_extract(_url, _settings, _registry=None):
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

    async def _fake_extract(_url, _settings, _registry=None):
        return extraction.ExtractionResult(markdown="x " * 500, metadata={})

    attempts = {"n": 0}

    async def _flaky_llm(_system, _user, _settings, **_kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise llm.LLMProviderError("transient 502")
        return (
            "First sentence with punctuation. "
            "Second sentence with punctuation. "
            "Third sentence with punctuation. "
        ) * 20

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    monkeypatch.setattr(llm, "generate", _flaky_llm)
    _stub_tts_and_audio(monkeypatch)

    # Speed the tenacity backoff.
    import tenacity

    monkeypatch.setattr(tenacity.wait_exponential, "__call__", lambda self, rs: 0)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "done"
    assert after.stage == "finalize"
    assert attempts["n"] >= 2  # retried at least once


async def test_pipeline_does_not_retry_on_llm_request_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4xx errors from the LLM (LLMRequestError) must NOT be retried -- the
    request is permanently malformed and retrying wastes time."""

    database.run_migrations(env)

    async def _fake_extract(_url, _settings, _registry=None):
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


async def test_pipeline_records_failure_when_exception_contains_curly_braces(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg stderr / JSON bodies / sentence previews routinely contain
    literal '{' or '}'. _finalize_failure must NOT pass these through
    str.format (which would raise KeyError mid-finalize and leave the job
    stuck in 'processing'). The fix uses str.replace so braces are inert."""

    database.run_migrations(env)
    _stub_full_chain(monkeypatch)

    async def _llm_with_braces(_system, _user, _settings, **_kwargs):
        # Return short output so MIN_CLEANUP_CHARS fires AND the resulting
        # error path embeds a brace via the cleanup stage's error string.
        return "short {curly braced} output"

    monkeypatch.setattr(llm, "generate", _llm_with_braces)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    # The fix must produce a recorded failure, not a stuck 'processing' job.
    assert after.status == "failed"
    assert after.stage == "cleanup"
    assert "MIN_CLEANUP_CHARS" in (after.error or "")


def _stub_artwork_download(monkeypatch: pytest.MonkeyPatch, png_bytes: bytes) -> None:
    """Patch httpx.AsyncClient so artwork.process_artwork serves ``png_bytes``,
    and bypass the SSRF resolver so example.test doesn't NXDOMAIN."""

    transport = httpx.MockTransport(lambda _r: httpx.Response(200, content=png_bytes))
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    from app.services import ssrf as _ssrf

    async def _stub_resolve(_host: str) -> str:
        return "203.0.113.1"

    monkeypatch.setattr(_ssrf, "resolve_public_host", _stub_resolve)


def _png_bytes(size: int = 800) -> bytes:
    img = Image.new("RGB", (size, size), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def test_pipeline_writes_artwork_jpg_and_reaches_transcript(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When extraction metadata carries ogImage, the artwork stage must write
    a JPG to ``{DATA_DIR}/media/{episode_id}.jpg`` and the pipeline must
    still terminate at stage='transcript'."""

    database.run_migrations(env)

    async def _fake_extract(_url, _settings, _registry=None):
        return extraction.ExtractionResult(
            markdown="raw " * 250,
            metadata={
                "title": "Example",
                "ogImage": "https://example.test/cover.png",
            },
        )

    async def _fake_llm(_system, _user, _settings, **_kwargs):
        return (
            "This is a cleaned narration sentence with proper punctuation. "
            "Each sentence ends with a period. "
            "Paragraphs are separated by blank lines.\n\n"
            "Here is another paragraph of cleaned narration text. "
            "The chunker can split on these boundaries. "
        ) * 10

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    monkeypatch.setattr(llm, "generate", _fake_llm)
    _stub_tts_and_audio(monkeypatch)
    _stub_artwork_download(monkeypatch, _png_bytes())

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "done"
    assert after.stage == "finalize"

    expected_jpg = env / "media" / f"{job.episode_id}.jpg"
    assert expected_jpg.exists()
    # Confirm it really is a JPG that Pillow can re-open at the configured size.
    out = Image.open(expected_jpg)
    out.load()
    assert out.format == "JPEG"
    assert out.size == (
        get_settings().ARTWORK_SIZE_PX,
        get_settings().ARTWORK_SIZE_PX,
    )


async def test_pipeline_succeeds_when_artwork_falls_back(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ogImage in metadata -> artwork returns None, no JPG written, but
    the pipeline must still complete at stage='transcript'."""

    database.run_migrations(env)
    _stub_full_chain(monkeypatch)  # metadata is {"title": "Example"} -- no ogImage

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "done"
    assert after.stage == "finalize"
    assert not (env / "media" / f"{job.episode_id}.jpg").exists()


async def test_pipeline_transcript_stage_builds_vtt_from_chunks(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capture the (chunks, silence_ms) call into transcript.build_vtt to
    confirm the pipeline threads the live chunk texts + TTS durations into
    the VTT builder."""

    database.run_migrations(env)
    _stub_full_chain(monkeypatch)

    captured: dict[str, object] = {}
    real_build = transcript.build_vtt

    def _spy_build(chunks, silence_ms):
        captured["chunks"] = list(chunks)
        captured["silence_ms"] = silence_ms
        return real_build(chunks, silence_ms)

    monkeypatch.setattr(pipeline.transcript, "build_vtt", _spy_build)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "done"
    assert after.stage == "finalize"

    chunks = captured["chunks"]
    assert isinstance(chunks, list) and chunks
    assert all(isinstance(c, transcript.TranscriptChunk) for c in chunks)
    # Stubbed TTS reports 1s per chunk; transcript stage receives those.
    assert all(c.duration_secs == 1.0 for c in chunks)
    # silence_ms must match the configured chunk silence so VTT timestamps
    # align with the produced MP3.
    assert captured["silence_ms"] == get_settings().TTS_CHUNK_SILENCE_MS


async def test_pipeline_transcript_stage_rejects_length_mismatch(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If anything between chunk and tts corrupts the in-memory state so the
    two lists desync, the transcript stage must fail loudly with a clear
    message (not a stdlib zip error) and stop at stage='transcript'."""

    database.run_migrations(env)
    _stub_full_chain(monkeypatch)

    from app.services import tts as tts_module

    async def _drop_one(text, episode_id, chunk_index, settings, seed=None, verify=False):
        result = await _stub_tts_for_extra(text, episode_id, chunk_index, settings)
        return result

    async def _stub_tts_for_extra(text, episode_id, chunk_index, settings, seed=None, verify=False):
        return tts_module.GenerateResult(
            wav_path=f"/tmp/{episode_id}_chunk_{chunk_index}.wav",
            duration_secs=1.0,
            sample_rate=24000,
        )

    call_count = {"n": 0}
    real_tts = _drop_one

    async def _short_tts(text, episode_id, chunk_index, settings, seed=None, verify=False):
        call_count["n"] += 1
        # Return only for first call; subsequent calls still execute but the
        # test patches _stage_transcript's inputs by intercepting the chunker.
        return await real_tts(text, episode_id, chunk_index, settings)

    monkeypatch.setattr(tts_module, "generate_chunk_with_retry", _short_tts)

    real_stage = pipeline._stage_tts

    async def _truncate_tts(job, chunks, settings):
        results = await real_stage(job, chunks, settings)
        return results[:-1]  # drop one to force a length mismatch

    monkeypatch.setattr(pipeline, "_stage_tts", _truncate_tts)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "failed"
    assert after.stage == "transcript"
    assert "transcript stage:" in (after.error or "")
    assert "pipeline state corrupted" in (after.error or "")


async def test_pipeline_finalize_upserts_episode_row(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 7: finalize must write an episodes row carrying the live
    audio/artwork/vtt/duration produced by the prior stages."""

    database.run_migrations(env)
    _stub_full_chain(monkeypatch)

    async def _fake_extract_with_title(_url, _settings, _registry=None):
        return extraction.ExtractionResult(
            markdown="raw " * 250,
            metadata={"title": "Test Article", "author": "Test Author"},
        )

    monkeypatch.setattr(extraction, "extract", _fake_extract_with_title)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "done"
    assert after.stage == "finalize"

    from app.services import episodes as episodes_service

    conn = database.connect(database.db_path(env))
    try:
        row = episodes_service.get_by_id(conn, job.episode_id)
    finally:
        conn.close()

    assert row is not None
    assert row.title == "Test Article"
    assert row.author == "Test Author"
    assert row.original_url == job.url
    assert row.audio_path and row.audio_path.endswith(f"/{job.episode_id}.mp3")
    # No ogImage in metadata -> artwork falls back to feed-level art.
    assert row.artwork_path is None
    assert row.transcript_vtt and row.transcript_vtt.startswith("WEBVTT")
    # Stubbed encode returns 2.5s; round() uses banker's rounding -> 2.
    assert row.duration_secs == 2


async def test_pipeline_finalize_falls_back_author_to_feed_author(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the article has no author, the episode row uses FEED_AUTHOR so
    the iTunes author field stays populated."""

    database.run_migrations(env)
    _stub_full_chain(monkeypatch)

    async def _fake_extract_no_author(_url, _settings, _registry=None):
        return extraction.ExtractionResult(
            markdown="raw " * 250,
            metadata={"title": "No Byline"},
        )

    monkeypatch.setattr(extraction, "extract", _fake_extract_no_author)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    from app.services import episodes as episodes_service

    conn = database.connect(database.db_path(env))
    try:
        row = episodes_service.get_by_id(conn, job.episode_id)
    finally:
        conn.close()

    assert row is not None
    assert row.author == get_settings().FEED_AUTHOR


async def test_pipeline_transcript_stage_failure_marks_job_failed(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If build_vtt raises, the failure must be persisted with
    stage='transcript' rather than leaving the job stuck in 'processing'."""

    database.run_migrations(env)
    _stub_full_chain(monkeypatch)

    def _boom(_chunks, _silence_ms):
        raise RuntimeError("vtt builder exploded")

    monkeypatch.setattr(pipeline.transcript, "build_vtt", _boom)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "failed"
    assert after.stage == "transcript"
    assert "vtt builder exploded" in (after.error or "")


async def test_pipeline_audio_stage_cleans_up_intermediate_wavs(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The finally block in _stage_audio must remove the combined WAV and
    each per-chunk WAV regardless of success or failure. Without an explicit
    test, a future refactor could silently drop the cleanup."""

    database.run_migrations(env)
    _stub_full_chain(monkeypatch)

    # Track which paths the cleanup helper sees.
    seen: list[Path] = []
    real_remove = pipeline.audio.remove_quietly

    def _spy_remove(*paths):
        seen.extend(paths)
        return real_remove(*paths)

    monkeypatch.setattr(pipeline.audio, "remove_quietly", _spy_remove)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "done"
    # The combined wav + at least one chunk wav must have been passed in.
    assert any(p.name.endswith("_combined.wav") for p in seen)
    assert any(p.name.endswith("_chunk_0.wav") for p in seen)


# --- corrections stage: seed + user merge ----------------------------------


def test_corrections_applies_seed_brand_phrase(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An applicable seed row (a brand phrase) is corrected even with no user
    dictionary present."""

    database.run_migrations(env)  # no user dict stored -> empty user corrections
    out = asyncio.run(
        pipeline._apply_corrections("I bought a Louis Vuitton bag.", get_settings())
    )
    assert "loo-ee vwee-tohn" in out


def test_corrections_user_override_beats_seed(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user correction whose key also exists in the seed wins."""

    from app.services import lexicon

    database.run_migrations(env)
    with database.connection(env) as conn:
        lexicon.replace_user_entries(
            conn, {"Louis Vuitton": {"mode": "override", "spoken": "ELL VEE"}}
        )
    out = asyncio.run(pipeline._apply_corrections("My Louis Vuitton bag.", get_settings()))
    assert "ELL VEE" in out
    assert "loo-ee vwee-tohn" not in out


def test_corrections_user_entry_applies_via_lexicon(env: Path) -> None:
    """A user lexicon entry is applied by the deterministic backstop."""

    from app.services import lexicon

    database.run_migrations(env)
    with database.connection(env) as conn:
        lexicon.replace_user_entries(
            conn, {"widget": {"mode": "override", "spoken": "wid jet"}}
        )
    out = asyncio.run(pipeline._apply_corrections("a widget here", get_settings()))
    assert "wid jet" in out


def test_base_lexicon_confidence_gate(env: Path) -> None:
    """Aggressive base-lexicon apply uses high-confidence base rows but skips
    low-confidence ones, so noisy data (e.g. WikiAbbrev's 'the' -> 'these') can't
    clobber prose."""

    from app.services import lexicon

    database.run_migrations(env)
    with database.connection(env) as conn:
        lexicon.import_readonly(
            conn,
            "base",
            {
                # noisy crowd abbreviation below the gate -> must NOT apply
                "the": {"mode": "override", "spoken": "these", "confidence": 0.4},
                # high-confidence override -> applies
                "Qatari": {"mode": "override", "spoken": "kuh-TAR-ee", "confidence": 1.0},
            },
        )
        conn.commit()
    out = asyncio.run(pipeline._apply_corrections("the Qatari team", get_settings()))
    assert "these" not in out  # low-confidence noise gated out
    assert "the" in out  # left intact
    assert "kuh-TAR-ee" in out  # high-confidence base row applied


# --- normalize stage: LLM pronunciation pass + deterministic backstop -------


def _echo_window(seen: dict):
    """Stub _llm_with_retry that returns the window text unchanged (LLM no-op),
    so the test isolates the deterministic backstop and pass plumbing."""

    async def _echo(_system, user, _settings, **_kwargs):
        seen["called"] = seen.get("called", 0) + 1
        return user.split("<text>\n", 1)[1].rsplit("\n</text>", 1)[0]

    return _echo


async def test_normalize_runs_llm_pass_then_deterministic_backstop(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stage calls the LLM pronunciation pass and then applies the seed
    dictionary as the guaranteed backstop."""

    database.run_migrations(env)
    seen: dict = {}
    monkeypatch.setattr(pipeline, "_llm_with_retry", _echo_window(seen))
    job = _seed_job(env)
    out = await pipeline._stage_normalize(
        job, "I shopped for a Louis Vuitton bag today.", get_settings()
    )
    assert seen.get("called")  # the LLM pass ran
    assert "loo-ee vwee-tohn" in out  # backstop applied the seed correction


async def test_normalize_llm_short_output_falls_back_to_input(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window whose LLM output is far shorter than its input is discarded so
    the pass never drops article content."""

    database.run_migrations(env)
    monkeypatch.setattr(pipeline.chunker, "pack_paragraphs", lambda _t, _n: None)

    async def _truncate(_system, _user, _settings, **_kwargs):
        return "x"  # pathologically short

    monkeypatch.setattr(pipeline, "_llm_with_retry", _truncate)
    long_text = "The council approved the budget after a long debate. " * 20
    out = await pipeline._pronounce_with_llm("job", long_text, get_settings())
    assert out == long_text  # original window preserved, not the "x"


async def test_normalize_llm_strips_preamble_via_marker_contract(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that prepends conversational commentary but wraps the respelled
    text in the begin/end markers must have the commentary dropped, not narrated."""

    database.run_migrations(env)
    monkeypatch.setattr(pipeline.chunker, "pack_paragraphs", lambda _t, _n: None)
    body = "The council approved the budget after a long debate. " * 20

    async def _preamble(_system, _user, _settings, **_kwargs):
        return (
            "No pronunciation reference was provided alongside the text, so there "
            "are no terms to change. Here is the text reproduced in full:\n\n"
            f"<<<AUDICLE_BEGIN>>>\n{body}\n<<<AUDICLE_END>>>"
        )

    monkeypatch.setattr(pipeline, "_llm_with_retry", _preamble)
    out = await pipeline._pronounce_with_llm("job", body, get_settings())
    assert "reproduced in full" not in out
    assert "No pronunciation reference" not in out
    assert "council approved the budget" in out


async def test_normalize_llm_retries_when_model_ignores_markers(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first reply with no markers (preamble glued onto the bare text) is retried
    once; the retry's marker'd content is what survives, with the preamble dropped."""

    database.run_migrations(env)
    monkeypatch.setattr(pipeline.chunker, "pack_paragraphs", lambda _t, _n: None)
    body = "The council approved the budget after a long debate. " * 20
    outputs = iter(
        [
            "No pronunciation reference was provided, so there are no terms to "
            "change. Here is the text reproduced in full:\n\n" + body,
            f"<<<AUDICLE_BEGIN>>>\n{body}\n<<<AUDICLE_END>>>",
        ]
    )
    calls = {"n": 0}

    async def _fake(_system, _user, _settings, **_kwargs):
        calls["n"] += 1
        return next(outputs)

    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    out = await pipeline._pronounce_with_llm("job", body, get_settings())
    assert calls["n"] == 2  # retried once when the first reply had no markers
    assert "reproduced in full" not in out
    assert "council approved the budget" in out


async def test_normalize_llm_no_markers_keeps_window_verbatim(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the model never emits markers (even on retry), the window is kept
    verbatim -- a real first paragraph that opens like a preamble ("Here is...")
    must not be dropped by the cleanup preamble heuristic."""

    database.run_migrations(env)
    monkeypatch.setattr(pipeline.chunker, "pack_paragraphs", lambda _t, _n: None)
    window = "Here is the key finding.\n\n" + "The budget passed after a long debate. " * 20

    async def _no_markers(_system, _user, _settings, **_kwargs):
        return window  # never wraps in markers, on either attempt

    monkeypatch.setattr(pipeline, "_llm_with_retry", _no_markers)
    out = await pipeline._pronounce_with_llm("job", window, get_settings())
    assert "Here is the key finding" in out  # not stripped as a preamble
    assert "budget passed after a long debate" in out


# --- cleanup: boilerplate-only window drop ---------------------------------


async def test_cleanup_drops_boilerplate_only_windows(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows that come back as the sentinel or a disclaimer are dropped; only
    the real article body survives into the cleaned text."""

    database.run_migrations(env)
    monkeypatch.setattr(pipeline.chunker, "pack_paragraphs", lambda _md, _n: ["a", "b", "c"])
    outputs = iter(
        [
            "NO_ARTICLE_CONTENT",
            "The mayor announced the budget today and the council approved it. " * 8,
            "There is no article body text in the content you provided.",
        ]
    )

    async def _fake(_system, _user, _settings, **_kwargs):
        return next(outputs)

    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    cleaned = await pipeline._stage_cleanup("job", "markdown", get_settings())
    assert "NO_ARTICLE_CONTENT" not in cleaned
    assert "there is no article" not in cleaned.lower()
    assert "mayor announced the budget" in cleaned


# --- cleanup: sentinel-marker output contract (integration) ----------------


async def test_cleanup_retries_when_model_ignores_markers(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window that comes back as a bare conversational refusal (no markers, no
    sentinel) is retried once; the retry's marker'd content is what survives."""

    database.run_migrations(env)
    monkeypatch.setattr(pipeline.chunker, "pack_paragraphs", lambda _md, _n: ["only-window"])
    outputs = iter(
        [
            "I don't have any stored instructions. Could you point me to the rules?",
            "<<<AUDICLE_BEGIN>>>\n"
            + "The mayor announced the budget today and the council approved it. " * 8
            + "\n<<<AUDICLE_END>>>",
        ]
    )
    calls = {"n": 0}

    async def _fake(_system, _user, _settings, **_kwargs):
        calls["n"] += 1
        return next(outputs)

    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    cleaned = await pipeline._stage_cleanup("job", "markdown", get_settings())
    assert calls["n"] == 2  # initial refusal + one retry
    assert "mayor announced the budget" in cleaned
    assert "stored instructions" not in cleaned


async def test_cleanup_extracts_marker_body_dropping_preamble(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CoreWeave incident: preamble glued on top of real marker'd content is
    stripped, the content kept, and no retry fires (markers were honored)."""

    database.run_migrations(env)
    monkeypatch.setattr(pipeline.chunker, "pack_paragraphs", lambda _md, _n: ["w1"])
    calls = {"n": 0}

    async def _fake(_system, _user, _settings, **_kwargs):
        calls["n"] += 1
        return (
            "I don't have any stored instructions for how to clean articles.\n\n"
            "<<<AUDICLE_BEGIN>>>\n"
            + "CoreWeave is a cloud computing company that runs GPU data centers. " * 8
            + "\n<<<AUDICLE_END>>>"
        )

    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    cleaned = await pipeline._stage_cleanup("job", "markdown", get_settings())
    assert calls["n"] == 1  # markers honored -> no retry
    assert "stored instructions" not in cleaned
    assert "CoreWeave is a cloud computing company" in cleaned


# --- cleanup: residual markdown heading strip ------------------------------


def test_strip_heading_markers_removes_atx_headings_only() -> None:
    text = "### Getting started\nBody.\n## Sub\ntext with C# inline\n#nospace"
    out = pipeline._strip_heading_markers(text)
    # Heading hashes (with trailing space) are removed; inline # and a hash with
    # no following space are left alone.
    assert out == "Getting started\nBody.\nSub\ntext with C# inline\n#nospace"


def test_normalize_date_months_only_in_date_context() -> None:
    assert pipeline._normalize_date_months("Jan 15 2026") == "January 15 2026"
    assert pipeline._normalize_date_months("Feb. 3, 2020") == "February 3, 2020"
    assert pipeline._normalize_date_months("Sept 9 and Nov 3") == "September 9 and November 3"
    # Not followed by a number -> left alone (often a name).
    assert pipeline._normalize_date_months("Jan and Mar met Aug") == "Jan and Mar met Aug"
    assert pipeline._normalize_date_months("Janet 5 ate") == "Janet 5 ate"


def test_normalize_for_tts_strips_headings_and_expands_dates() -> None:
    # "Jan" expands to the full month, then the month normalizer respells it.
    out = pipeline._normalize_for_tts("### News\nShipped Jan 15 2026.")
    assert out == "News\nShipped jan-yoo-air-ee 15 2026."


def test_normalize_numbers_spells_grouped_and_decimal() -> None:
    assert pipeline._normalize_numbers("price 1,000 today") == "price one thousand today"
    assert "one million" in pipeline._normalize_numbers("1,234,567 rows")
    assert pipeline._normalize_numbers("pi is 3.14") == "pi is three point one four"


def test_normalize_numbers_grouped_with_decimal() -> None:
    # Grouped thousands carrying a decimal fraction must spell as one number,
    # not leave a stray "1," with the fraction mis-spoken.
    assert (
        pipeline._normalize_numbers("cost 1,234.56 dollars")
        == "cost one thousand two hundred thirty-four point five six dollars"
    )


def test_normalize_numbers_preserves_trailing_zero_fraction() -> None:
    # Fractions read digit-by-digit so "2.0"/"1.50" don't collapse to integers.
    assert pipeline._normalize_numbers("Web 2.0 era") == "Web two point zero era"
    assert pipeline._normalize_numbers("just 1.50 left") == "just one point five zero left"


def test_normalize_numbers_spells_sentence_ending_number() -> None:
    # A number immediately followed by a sentence-ending period must still be
    # spelled; the trailing-dot guard only protects dotted versions/IPs.
    assert (
        pipeline._normalize_numbers("The cost was 1,234.56.")
        == "The cost was one thousand two hundred thirty-four point five six."
    )
    assert pipeline._normalize_numbers("GDP grew to 3.14.") == "GDP grew to three point one four."
    assert pipeline._normalize_numbers("I weigh 220.5.") == "I weigh two hundred twenty point five."


def test_normalize_numbers_leaves_ambiguous_and_glued_alone() -> None:
    # Bare integers (day/year), versions, IPs, and code-glued digits are
    # context-dependent -- left to the LLM prompt, untouched here.
    assert pipeline._normalize_numbers("In 2026 we saw 15 things") == "In 2026 we saw 15 things"
    assert pipeline._normalize_numbers("version 1.2.3 shipped") == "version 1.2.3 shipped"
    assert pipeline._normalize_numbers("ip 10.0.0.1 here") == "ip 10.0.0.1 here"
    assert pipeline._normalize_numbers("x86 and startup_32") == "x86 and startup_32"


def test_normalize_dotted_acronyms() -> None:
    # The engine pauses on the periods, so collapse them to spaced letters.
    assert pipeline._normalize_dotted_acronyms("about A.I. today") == "about A I today"
    assert pipeline._normalize_dotted_acronyms("the U.S.A. and U.K.") == "the U S A and U K"
    # Lowercase latin abbreviations and decimals/versions are left untouched.
    assert pipeline._normalize_dotted_acronyms("e.g. this, i.e. that") == "e.g. this, i.e. that"
    assert pipeline._normalize_dotted_acronyms("v1.2 and pi 3.14") == "v1.2 and pi 3.14"


def test_normalize_months_respells_capitalized_only() -> None:
    assert pipeline._normalize_months("Posted February 3") == "Posted feb-roo-air-ee 3"
    assert pipeline._normalize_months("in January 2026") == "in jan-yoo-air-ee 2026"
    assert pipeline._normalize_months("by October") == "by ock-toh-ber"
    # Lowercase homographs (adjective/verb/modal) are NOT touched.
    assert pipeline._normalize_months("an august institution") == "an august institution"
    assert pipeline._normalize_months("they may go") == "they may go"
    assert pipeline._normalize_months("soldiers march on") == "soldiers march on"


def test_normalize_acronyms_spells_unknown_allcaps() -> None:
    # Unknown all-caps (tickers, unfamiliar acronyms) spelled letter by letter.
    assert pipeline._normalize_acronyms("the CRWV stock") == "the C R W V stock"
    assert pipeline._normalize_acronyms("buy NVDA now") == "buy N V D A now"
    # Letters with a trailing digit: digit read as a word.
    assert pipeline._normalize_acronyms("SSE2 support") == "S S E two support"


def test_normalize_acronyms_handles_plurals() -> None:
    assert pipeline._normalize_acronyms("many GPUs here") == "many G P yoos here"
    assert pipeline._normalize_acronyms("two APIs") == "two A P eyes"
    assert pipeline._normalize_acronyms("the URLs") == "the U R els"
    assert pipeline._normalize_acronyms("three SDKs") == "three S D kays"


def test_normalize_acronyms_keeps_read_as_word_and_singletons() -> None:
    # Read-as-word acronyms are left intact.
    assert pipeline._normalize_acronyms("NASA and NATO") == "NASA and NATO"
    assert pipeline._normalize_acronyms("COVID cases") == "COVID cases"
    # Already-spaced single letters are not re-spelled.
    assert pipeline._normalize_acronyms("G P U here") == "G P U here"
    # Mixed-case tokens are left to the lexicon (not letter-spelled).
    assert pipeline._normalize_acronyms("OAuth and IPv6") == "OAuth and IPv6"


def test_normalize_ranges_converts_dash_to_word() -> None:
    # Spaced and unspaced digit ranges, hyphen and en/em dash.
    assert pipeline._normalize_ranges("from 2017 - 2021") == "from 2017 to 2021"
    assert pipeline._normalize_ranges("2017-2021") == "2017 to 2021"
    assert pipeline._normalize_ranges("pages 10-12") == "pages 10 to 12"
    assert pipeline._normalize_ranges("5\u201310 minutes") == "5 to 10 minutes"
    assert pipeline._normalize_ranges("rated 4\u20145 stars") == "rated 4 to 5 stars"


def test_normalize_ranges_leaves_dates_and_chains_alone() -> None:
    # ISO dates and dashed phone chains (3+ numbers) are not ranges.
    assert pipeline._normalize_ranges("on 2024-01-15 today") == "on 2024-01-15 today"
    assert pipeline._normalize_ranges("call 1-800-555-1234") == "call 1-800-555-1234"
    # Hyphenated words are untouched (no digits).
    assert pipeline._normalize_ranges("a well-known fact") == "a well-known fact"
    # A grouped-number range is spelled by the later number pass.
    assert "one thousand to two thousand" in pipeline._normalize_for_tts("1,000-2,000 units")


def test_strip_code_artifacts_removes_backticks_parens_and_hex() -> None:
    assert pipeline._strip_code_artifacts("call `smp init()` now") == "call smp init now"
    assert (
        pipeline._strip_code_artifacts("loaded at 0x1000000 then 0xffffffff81000000")
        == "loaded at a hexadecimal value then a hexadecimal value"
    )
    # Plain prose (with real parenthetical) is untouched.
    assert pipeline._strip_code_artifacts("a normal sentence (with an aside)") == (
        "a normal sentence (with an aside)"
    )


def test_normalize_for_tts_strips_code_artifacts() -> None:
    out = pipeline._normalize_for_tts("The `__init` section maps 0x1000000 in ram.")
    assert "`" not in out
    assert "0x" not in out
    assert "a hexadecimal value" in out


def test_normalize_currency_expands_magnitude_suffix() -> None:
    assert pipeline._normalize_currency("raised $500k") == "raised five hundred thousand dollars"
    assert (
        pipeline._normalize_currency("a $3.5M round")
        == "a three point five million dollars round"
    )
    assert pipeline._normalize_currency("worth $1.2B now") == "worth one point two billion dollars now"


def test_normalize_currency_plain_and_grouped_and_symbols() -> None:
    assert pipeline._normalize_currency("costs $500") == "costs five hundred dollars"
    assert (
        pipeline._normalize_currency("paid $1,200 total")
        == "paid one thousand two hundred dollars total"
    )
    assert pipeline._normalize_currency("about €100k") == "about one hundred thousand euros"
    assert pipeline._normalize_currency("won £2M") == "won two million pounds"


def test_normalize_currency_expands_magnitude_word() -> None:
    # A magnitude written as a word after a space ("$3 million") must read
    # "three million dollars", not "three dollars million".
    assert pipeline._normalize_currency("raised $3 million") == "raised three million dollars"
    assert (
        pipeline._normalize_currency("a $3.5 billion round")
        == "a three point five billion dollars round"
    )
    assert pipeline._normalize_currency("$500 thousand") == "five hundred thousand dollars"
    assert pipeline._normalize_currency("worth €2 trillion") == "worth two trillion euros"


def test_normalize_currency_word_magnitude_respects_word_boundary() -> None:
    # "millionaire" is not a magnitude: only "$3" expands, the noun is left alone.
    assert pipeline._normalize_currency("a $3 millionaire") == "a three dollars millionaire"


def test_normalize_currency_leaves_unitted_bare_numbers_alone() -> None:
    # No currency symbol means the suffix is a unit, left to the LLM prompt.
    assert pipeline._normalize_currency("500m north") == "500m north"
    assert pipeline._normalize_currency("a 5k run") == "a 5k run"
    # A mis-suffixed token ($500kg) falls through untouched rather than mis-read.
    assert pipeline._normalize_currency("weighs $500kg") == "weighs $500kg"


def test_normalize_currency_preserves_trailing_punctuation() -> None:
    # The thousands-grouped number shape stops a trailing comma/period being
    # swallowed into the amount and dropped from the sentence.
    assert pipeline._normalize_currency("was $500, but") == "was five hundred dollars, but"
    assert pipeline._normalize_currency("cost $1,200.") == "cost one thousand two hundred dollars."


def test_normalize_for_tts_runs_currency_before_numbers() -> None:
    # Ordered pass: currency expands first, leaving no digits for the number pass.
    assert pipeline._normalize_for_tts("worth $2.5B") == "worth two point five billion dollars"


def test_normalize_identifiers_expands_snake_case() -> None:
    assert pipeline._normalize_identifiers("startup_32 ran") == "startup 32 ran"
    assert pipeline._normalize_identifiers("__startup_64 entry") == "startup 64 entry"
    assert (
        pipeline._normalize_identifiers("reset_early_page_tables here")
        == "reset early page tables here"
    )


def test_normalize_identifiers_leaves_prose_and_files_alone() -> None:
    # Dotted file/framework names are left to the LLM cleanup rule, not respaced.
    assert pipeline._normalize_identifiers("a normal sentence.") == "a normal sentence."
    assert pipeline._normalize_identifiers("and/or maybe") == "and/or maybe"
    assert pipeline._normalize_identifiers("built on node.js today") == "built on node.js today"


# --- corrections: pronunciation after normalization ------------------------


def test_corrections_voices_february_after_date_normalization(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup normalizes 'Feb 3' -> 'February 3', then the month normalizer
    respells every month phonetically (January and February here)."""

    database.run_migrations(env)  # no user dict stored
    normalized = pipeline._normalize_for_tts("Posted Jan 15 2026 and Feb 3 2025.")
    out = asyncio.run(pipeline._apply_corrections(normalized, get_settings()))
    assert "jan-yoo-air-ee 15 2026" in out
    assert "feb-roo-air-ee 3 2025" in out


# --- audio-QA: per-chunk regeneration on bad audio -------------------------


def _write_drone_wav(path: Path) -> None:
    import numpy as np
    import soundfile as sf

    t = np.arange(int(24000 * 1.0)) / 24000
    sf.write(str(path), (0.5 * np.sin(2 * np.pi * 440 * t)).astype("float32"), 24000, subtype="PCM_16")


def _write_speechlike_wav(path: Path) -> None:
    import numpy as np
    import soundfile as sf

    t = np.arange(int(24000 * 2.0)) / 24000
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 4 * t)
    env[(t > 0.7) & (t < 0.95)] = 0.0
    sf.write(
        str(path),
        (0.6 * np.sin(2 * np.pi * 180 * t) * env).astype("float32"),
        24000,
        subtype="PCM_16",
    )


async def test_chunk_quality_check_regenerates_bad_chunk(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database.run_migrations(env)
    from app.services import tts

    wav = tmp_path / "ep_chunk_0.wav"  # wrapper reuses one path; re-gen overwrites
    calls = {"n": 0}
    seeds: list[int | None] = []

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False):
        calls["n"] += 1
        seeds.append(seed)
        _write_drone_wav(wav) if calls["n"] == 1 else _write_speechlike_wav(wav)
        return tts.GenerateResult(wav_path=str(wav), duration_secs=1.0, sample_rate=24000)

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)
    job = _seed_job(env)
    result = await pipeline._generate_chunk_quality_checked(
        job, "two words here now", 0, get_settings()
    )
    assert calls["n"] == 2  # one bad read, one regeneration that recovered
    assert result.wav_path == str(wav)
    # The baseline uses the wrapper's configured seed (no override); the regen
    # sends a distinct seed so Chatterbox produces different audio.
    assert seeds[0] is None
    assert isinstance(seeds[1], int) and seeds[1] != 0


async def test_chunk_quality_check_keeps_last_after_max_regen(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database.run_migrations(env)
    from app.services import tts

    wav = tmp_path / "ep_chunk_0.wav"
    calls = {"n": 0}

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False):
        calls["n"] += 1
        _write_drone_wav(wav)  # always bad
        return tts.GenerateResult(wav_path=str(wav), duration_secs=1.0, sample_rate=24000)

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)
    job = _seed_job(env)
    result = await pipeline._generate_chunk_quality_checked(
        job, "two words here now", 0, get_settings()
    )
    # 1 baseline + MAX_REGEN extra attempts; job is not failed (a result returns).
    assert calls["n"] == get_settings().AUDIO_ANALYSIS_MAX_REGEN + 1
    assert result.wav_path == str(wav)


async def test_chunk_quality_check_disabled_calls_once(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AUDIO_ANALYSIS_ENABLED", "false")
    get_settings.cache_clear()
    database.run_migrations(env)
    from app.services import tts

    wav = tmp_path / "ep_chunk_0.wav"
    calls = {"n": 0}

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False):
        calls["n"] += 1
        _write_drone_wav(wav)  # bad, but analysis is off so no regen
        return tts.GenerateResult(wav_path=str(wav), duration_secs=1.0, sample_rate=24000)

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)
    job = _seed_job(env)
    await pipeline._generate_chunk_quality_checked(
        job, "two words here now", 0, get_settings()
    )
    assert calls["n"] == 1


async def test_chunk_asr_verify_regenerates_on_divergence(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Isolate the ASR path: audio analysis off, whisper verify on.
    monkeypatch.setenv("AUDIO_ANALYSIS_ENABLED", "false")
    monkeypatch.setenv("WHISPER_VERIFY_ENABLED", "true")
    get_settings.cache_clear()
    database.run_migrations(env)
    from app.services import tts

    text = "this chunk has clearly more than eight spoken words in it"
    calls = {"n": 0}
    verifies: list[bool] = []

    async def _fake_tts(
        text, episode_id, chunk_index, settings, seed=None, verify=False
    ):
        calls["n"] += 1
        verifies.append(verify)
        # Diverge on the first attempt, match the asked-for text on the regen.
        transcript = (
            "totally different hallucinated nonsense audio here right now okay"
            if calls["n"] == 1
            else text
        )
        return tts.GenerateResult(
            wav_path=str(tmp_path / "ep_chunk_0.wav"),
            duration_secs=1.0,
            sample_rate=24000,
            transcript=transcript,
        )

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)
    job = _seed_job(env)
    result = await pipeline._generate_chunk_quality_checked(job, text, 0, get_settings())
    assert calls["n"] == 2  # diverged once, matched on regen
    assert all(verifies)  # verify flag sent on every attempt
    assert result.transcript == text


async def test_chunk_asr_verify_skips_short_chunk(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AUDIO_ANALYSIS_ENABLED", "false")
    monkeypatch.setenv("WHISPER_VERIFY_ENABLED", "true")
    get_settings.cache_clear()
    database.run_migrations(env)
    from app.services import tts

    calls = {"n": 0}
    verifies: list[bool] = []

    async def _fake_tts(
        text, episode_id, chunk_index, settings, seed=None, verify=False
    ):
        calls["n"] += 1
        verifies.append(verify)
        return tts.GenerateResult(
            wav_path=str(tmp_path / "ep_chunk_0.wav"),
            duration_secs=1.0,
            sample_rate=24000,
            transcript="totally unrelated transcript that would diverge",
        )

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)
    job = _seed_job(env)
    # 3 words is below WHISPER_VERIFY_MIN_WORDS (8): verify is not requested and
    # the divergent transcript is ignored, so there is no regeneration.
    await pipeline._generate_chunk_quality_checked(job, "three short words", 0, get_settings())
    assert calls["n"] == 1
    assert verifies == [False]
