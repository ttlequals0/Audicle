from __future__ import annotations

import asyncio
import io
from pathlib import Path

import httpx
import pytest
from app.config import get_settings
from app.core import database
from app.services import extraction, jobs, llm, pipeline, transcript
from PIL import Image


def _stub_full_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub extract + llm.generate so Phase 3 pipeline tests don't reach the
    network. Cleanup output is long enough to clear MIN_CLEANUP_CHARS."""

    async def _fake_extract(_url, _settings):
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

    async def _fake_tts(text, episode_id, chunk_index, settings):
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

    async def _fake_tts(text, episode_id, chunk_index, settings):
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

    from app.services import artwork as _artwork

    async def _allow_all(_host: str) -> None:
        return None

    monkeypatch.setattr(_artwork, "_assert_public_host", _allow_all)


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

    async def _fake_extract(_url, _settings):
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

    async def _drop_one(text, episode_id, chunk_index, settings):
        result = await _stub_tts_for_extra(text, episode_id, chunk_index, settings)
        return result

    async def _stub_tts_for_extra(text, episode_id, chunk_index, settings):
        return tts_module.GenerateResult(
            wav_path=f"/tmp/{episode_id}_chunk_{chunk_index}.wav",
            duration_secs=1.0,
            sample_rate=24000,
        )

    call_count = {"n": 0}
    real_tts = _drop_one

    async def _short_tts(text, episode_id, chunk_index, settings):
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

    async def _fake_extract_with_title(_url, _settings):
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

    async def _fake_extract_no_author(_url, _settings):
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
