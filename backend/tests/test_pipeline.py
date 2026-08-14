from __future__ import annotations

import asyncio
import io
import logging
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

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    monkeypatch.setattr(llm, "generate", _fake_llm)
    _stub_tts_and_audio(monkeypatch)


def _stub_tts_and_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub only the tts + audio touch points (re-used by tests that compose
    their own llm/extract stubs)."""

    from app.services import audio, tts

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
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
        return output_path, 24000, [1.0] * len(_paths)

    def _fake_encode(_input_wav, output_mp3, _settings):
        output_mp3.parent.mkdir(parents=True, exist_ok=True)
        output_mp3.write_bytes(b"FAKE_MP3")
        return audio.EncodeResult(mp3_path=output_mp3, duration_secs=2.5)

    # _stage_tts always selects a voice (the autouse fixture fills slot 1), and
    # the real call retries against the unreachable test host with real sleeps.
    async def _fake_select(_settings, _slot) -> None:
        pass

    monkeypatch.setattr(tts, "select_voice_with_retry", _fake_select)
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


async def test_pipeline_marks_failed_when_a_stage_stops_making_progress(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stage that goes silent for longer than the stall window is killed, and
    the error names the stall rather than an elapsed-time budget."""

    database.run_migrations(env)

    async def _slow_extract(url, settings, registry=None):
        await asyncio.sleep(2)
        return extraction.ExtractionResult(markdown="x" * 1000, metadata={})

    monkeypatch.setattr(extraction, "extract", _slow_extract)
    monkeypatch.setenv("JOB_STALL_SECONDS", "0.05")
    get_settings.cache_clear()

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "failed"
    assert after.stage == "extract"
    assert "made no progress" in (after.error or "")
    assert "JOB_STALL_SECONDS" in (after.error or "")


def test_effective_job_timeout_scales_with_chunk_count() -> None:
    s = SimpleNamespace(JOB_TIMEOUT_SECONDS=3600.0, JOB_TIMEOUT_PER_CHUNK_SECONDS=30.0)
    assert pipeline.effective_job_timeout(s, 0) == 3600.0
    assert pipeline.effective_job_timeout(s, 100) == 3600.0  # base floor wins
    assert pipeline.effective_job_timeout(s, 200) == 6000.0  # chunk-scaled wins


def test_job_ceiling_multiplies_the_chunk_scaled_estimate() -> None:
    s = SimpleNamespace(
        JOB_TIMEOUT_SECONDS=3600.0,
        JOB_TIMEOUT_PER_CHUNK_SECONDS=30.0,
        JOB_TIMEOUT_CEILING_MULTIPLIER=3.0,
    )
    assert pipeline.job_ceiling(s, 0) == 10800.0
    assert pipeline.job_ceiling(s, 200) == 18000.0


async def test_pipeline_survives_past_the_estimate_while_it_keeps_progressing(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this change exists for: a job whose TTS stage runs well
    past max(base, chunks x per-chunk) still finishes, because every completed
    chunk resets the stall window. The old wall-clock budget killed it mid-TTS."""

    database.run_migrations(env)
    _stub_full_chain(monkeypatch)

    from app.services import chunker, tts

    monkeypatch.setattr(chunker, "chunk", lambda *a, **k: ["a sentence."] * 10)

    async def _slow_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        await asyncio.sleep(0.08)
        return tts.GenerateResult(
            wav_path=f"/tmp/{episode_id}_chunk_{chunk_index}.wav",
            duration_secs=1.0,
            sample_rate=24000,
        )

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _slow_tts)
    # Estimate is max(0.5, 10 x 0.01) = 0.5s and TTS alone needs ~0.8s, so the
    # old model would have killed this. Ceiling is 10s and no chunk takes longer
    # than the 0.5s stall window, so the watchdog lets it run.
    monkeypatch.setenv("JOB_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("JOB_TIMEOUT_PER_CHUNK_SECONDS", "0.01")
    monkeypatch.setenv("JOB_TIMEOUT_CEILING_MULTIPLIER", "20")
    monkeypatch.setenv("JOB_STALL_SECONDS", "0.5")
    get_settings.cache_clear()

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "done"
    assert after.error is None


async def test_pipeline_fails_at_the_ceiling_even_while_progressing(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Progress is not a licence to run forever: a job that keeps beating still
    dies at the absolute ceiling, and the error says so rather than blaming a
    stall."""

    database.run_migrations(env)
    _stub_full_chain(monkeypatch)

    from app.services import chunker, tts

    monkeypatch.setattr(chunker, "chunk", lambda *a, **k: ["a sentence."] * 5)

    async def _very_slow_tts(
        text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw
    ):
        await asyncio.sleep(0.3)
        return tts.GenerateResult(
            wav_path=f"/tmp/{episode_id}_chunk_{chunk_index}.wav",
            duration_secs=1.0,
            sample_rate=24000,
        )

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _very_slow_tts)
    # Ceiling = max(1.0, 5 x 0.01) x 1 = 1.0s; TTS needs ~1.5s, so the ceiling
    # lands mid-TTS with a full second of slack for the stubbed stages ahead of
    # it. Every chunk beats well inside the 5s stall window, so only the ceiling
    # can fire.
    monkeypatch.setenv("JOB_TIMEOUT_SECONDS", "1.0")
    monkeypatch.setenv("JOB_TIMEOUT_PER_CHUNK_SECONDS", "0.01")
    monkeypatch.setenv("JOB_TIMEOUT_CEILING_MULTIPLIER", "1")
    monkeypatch.setenv("JOB_STALL_SECONDS", "5")
    get_settings.cache_clear()

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "failed"
    assert after.stage == "tts"
    assert "ceiling" in (after.error or "")
    assert "5 chunks" in (after.error or "")


async def test_pipeline_marks_failed_when_cleanup_returns_too_short(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MIN_CLEANUP_CHARS guard fires when BOTH the cleanup output and the raw
    extraction are below the floor (a genuinely empty/boilerplate page the raw
    fallback can't rescue). Stage must be 'cleanup' and the error reference the
    limit. A substantial article that the LLM merely botches falls back to raw
    instead -- see test_cleanup_falls_back_to_raw_when_llm_deflects."""

    database.run_migrations(env)

    async def _fake_extract(_url, _settings, _registry=None):
        return extraction.ExtractionResult(markdown="cookie consent banner", metadata={})

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
        # Fail cleanup with an error string that contains literal braces (as
        # ffmpeg stderr / JSON bodies do); _finalize_failure must not str.format it.
        raise llm.LLMRequestError("provider rejected request: {unexpected: value}")

    monkeypatch.setattr(llm, "generate", _llm_with_braces)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    # The fix must produce a recorded failure, not a stuck 'processing' job, with
    # the literal braces preserved inertly (proof they weren't run through format).
    assert after.status == "failed"
    assert after.stage == "cleanup"
    assert "{unexpected: value}" in (after.error or "")


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


async def test_pipeline_embeds_cover_when_artwork_present(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With per-episode art, the pipeline embeds the artwork's downsized copy into
    the episode MP3 (Pocket Casts reads only embedded art)."""

    database.run_migrations(env)

    async def _fake_extract(_url, _settings, _registry=None):
        return extraction.ExtractionResult(
            markdown="raw " * 250,
            metadata={"title": "Example", "ogImage": "https://example.test/cover.png"},
        )

    async def _fake_llm(_system, _user, _settings, **_kwargs):
        return "A cleaned narration sentence. Another sentence here. " * 20

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    monkeypatch.setattr(llm, "generate", _fake_llm)
    _stub_tts_and_audio(monkeypatch)
    _stub_artwork_download(monkeypatch, _png_bytes())

    embedded: dict[str, object] = {}

    def _spy_embed(mp3_path, cover_jpg_bytes):
        embedded["mp3_path"] = mp3_path
        embedded["bytes"] = cover_jpg_bytes

    monkeypatch.setattr(pipeline.audio, "embed_cover", _spy_embed)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    assert _job_after(env, job.id).status == "done"
    assert embedded["mp3_path"] == env / "media" / f"{job.episode_id}.mp3"
    # The embed copy is a real JPEG at the configured embed size.
    img = Image.open(io.BytesIO(embedded["bytes"]))
    img.load()
    assert img.format == "JPEG"
    assert img.size == (get_settings().EMBED_ARTWORK_SIZE_PX,) * 2


async def test_pipeline_skips_cover_embed_on_fallback_art(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No per-episode art -> nothing to embed; the MP3 ships untagged and players
    fall back to feed art, so embed_cover must not be called."""

    database.run_migrations(env)
    _stub_full_chain(monkeypatch)  # no ogImage -> artwork returns None

    called = False

    def _spy_embed(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(pipeline.audio, "embed_cover", _spy_embed)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    assert _job_after(env, job.id).status == "done"
    assert called is False


async def test_pipeline_transcript_stage_builds_vtt_from_chunks(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capture the (chunks, silence_ms) call into transcript.build_vtt to
    confirm the pipeline threads the live chunk texts + audio-stage durations
    into the VTT builder."""

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
    # Stubbed concat reports 1s per chunk; transcript stage receives those.
    assert all(c.duration_secs == 1.0 for c in chunks)
    # silence_ms must match the configured chunk silence so VTT timestamps
    # align with the produced MP3.
    assert captured["silence_ms"] == get_settings().TTS_CHUNK_SILENCE_MS


async def test_pipeline_transcript_stage_rejects_length_mismatch(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If anything between chunking and the audio stage corrupts the in-memory
    state so the chunk texts and audio-stage durations desync, the transcript
    stage must fail loudly with a clear message (not a stdlib zip error) and
    stop at stage='transcript'."""

    database.run_migrations(env)
    _stub_full_chain(monkeypatch)

    from app.services import tts as tts_module

    async def _drop_one(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        result = await _stub_tts_for_extra(text, episode_id, chunk_index, settings)
        return result

    async def _stub_tts_for_extra(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        return tts_module.GenerateResult(
            wav_path=f"/tmp/{episode_id}_chunk_{chunk_index}.wav",
            duration_secs=1.0,
            sample_rate=24000,
        )

    call_count = {"n": 0}
    real_tts = _drop_one

    async def _short_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
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


def test_corrections_applies_kept_seed_swap(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An applicable seed row (a real-word swap) is corrected even with no user
    dictionary present."""

    database.run_migrations(env)  # no user dict stored -> empty user corrections
    out = asyncio.run(pipeline._apply_corrections("a SQL query runs", get_settings()))
    assert "sequel" in out


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


def test_corrections_leaves_acronyms_unspelled(env: Path) -> None:
    """The auto-speller was removed: common acronyms reach Chatterbox verbatim (it says
    them natively) instead of being letter-spaced ("C E O"). Plurals pass through too."""

    database.run_migrations(env)
    out = asyncio.run(
        pipeline._apply_corrections("The CEO bought a GPU for the LLM.", get_settings())
    )
    assert "CEO" in out and "GPU" in out and "LLM" in out
    assert "C E O" not in out and "G P U" not in out and "L L M" not in out
    plural = asyncio.run(pipeline._apply_corrections("many GPUs and APIs", get_settings()))
    assert "GPUs" in plural and "APIs" in plural


def test_corrections_word_mode_acronym_reads_as_word(env: Path) -> None:
    """A word-mode dictionary entry (SQL -> sequel) is applied as its spoken word, not
    letter by letter."""

    from app.services import lexicon

    database.run_migrations(env)
    with database.connection(env) as conn:
        lexicon.replace_user_entries(
            conn, {"SQL": {"mode": "word", "spoken": "sequel", "case_sensitive": True}}
        )
    out = asyncio.run(pipeline._apply_corrections("a SQL query runs", get_settings()))
    assert "sequel" in out
    assert "S Q L" not in out


def test_corrections_case_insensitive_user_override(env: Path) -> None:
    """A case-insensitive override entry keyed '404 media' hits '404 Media' in the
    article (the actual bug: corrections.apply was always case-sensitive)."""

    from app.services import lexicon

    database.run_migrations(env)
    with database.connection(env) as conn:
        lexicon.replace_user_entries(
            conn,
            {
                "404 media": {
                    "mode": "override",
                    "spoken": "four oh four media",
                    "case_sensitive": False,
                }
            },
        )
    out = asyncio.run(pipeline._apply_corrections("404 Media reported it.", get_settings()))
    assert "four oh four media" in out
    assert "404 Media" not in out


def test_corrections_leaves_former_respell_word_plain(env: Path) -> None:
    """The pseudo-phonetic respellings were removed, so a word that used to be respelled
    is now left plain for Chatterbox's native pronunciation."""

    database.run_migrations(env)
    out = asyncio.run(pipeline._apply_corrections("We run Kubernetes in prod.", get_settings()))
    assert "Kubernetes" in out
    assert "koo-ber-neh-tees" not in out


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
        job, ["I ran a SQL query today."], get_settings()
    )
    assert seen.get("called")  # the LLM pass ran
    assert "sequel" in out[0]  # backstop applied the seed correction


async def test_normalize_llm_short_output_falls_back_to_input(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window whose LLM output is far shorter than its input is discarded so
    the pass never drops article content."""

    database.run_migrations(env)

    async def _truncate(_system, _user, _settings, **_kwargs):
        return "x"  # pathologically short

    monkeypatch.setattr(pipeline, "_llm_with_retry", _truncate)
    long_text = "The council approved the SQL budget after a long debate. " * 20
    out = await pipeline._pronounce_chunks_with_llm("job", [long_text], get_settings())
    assert out == [long_text]  # original chunk preserved, not the "x"


async def test_normalize_llm_strips_preamble_via_marker_contract(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that prepends conversational commentary but wraps the respelled
    text in the begin/end markers must have the commentary dropped, not narrated."""

    database.run_migrations(env)
    body = "The council approved the SQL budget after a long debate. " * 20

    async def _preamble(_system, _user, _settings, **_kwargs):
        return (
            "No pronunciation reference was provided alongside the text, so there "
            "are no terms to change. Here is the text reproduced in full:\n\n"
            f"<<<AUDICLE_BEGIN>>>\n{body}\n<<<AUDICLE_END>>>"
        )

    monkeypatch.setattr(pipeline, "_llm_with_retry", _preamble)
    out = "\n\n".join(
        await pipeline._pronounce_chunks_with_llm("job", [body], get_settings())
    )
    assert "reproduced in full" not in out
    assert "No pronunciation reference" not in out
    assert "council approved the SQL budget" in out


async def test_normalize_llm_retries_when_model_ignores_markers(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first reply with no markers (preamble glued onto the bare text) is retried
    once; the retry's marker'd content is what survives, with the preamble dropped."""

    database.run_migrations(env)
    body = "The council approved the SQL budget after a long debate. " * 20
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
    out = "\n\n".join(
        await pipeline._pronounce_chunks_with_llm("job", [body], get_settings())
    )
    assert calls["n"] == 2  # retried once when the first reply had no markers
    assert "reproduced in full" not in out
    assert "council approved the SQL budget" in out


async def test_normalize_llm_no_markers_keeps_window_verbatim(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the model never emits markers (even on retry), the window is kept
    verbatim -- a real first paragraph that opens like a preamble ("Here is...")
    must not be dropped by the cleanup preamble heuristic."""

    database.run_migrations(env)
    window = "Here is the key finding.\n\n" + "The SQL budget passed after a long debate. " * 20

    async def _no_markers(_system, _user, _settings, **_kwargs):
        return window  # never wraps in markers, on either attempt

    monkeypatch.setattr(pipeline, "_llm_with_retry", _no_markers)
    out = "\n\n".join(
        await pipeline._pronounce_chunks_with_llm("job", [window], get_settings())
    )
    assert "Here is the key finding" in out  # not stripped as a preamble
    assert "SQL budget passed after a long debate" in out


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


async def test_cleanup_falls_back_to_deterministic_cleaner_when_llm_deflects(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the model deflects into chat (no markers) but the extraction had a real
    article, cleanup ships the deterministically-cleaned body rather than failing."""

    database.run_migrations(env)
    article = "The council met on Tuesday to discuss the new budget proposal. " * 8
    monkeypatch.setattr(pipeline.chunker, "pack_paragraphs", lambda _md, _n: [article])

    async def _deflect(_system, _user, _settings, **_kwargs):
        return "Interesting article! Would you like a summary or something else?"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _deflect)
    cleaned = await pipeline._stage_cleanup("job", article, get_settings())
    assert "council met on Tuesday" in cleaned  # body fell through the fallback


async def test_cleanup_ships_real_article_when_model_refuses_via_no_article(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Register scenario: the model refuses a real article by emitting
    NO_ARTICLE_CONTENT. Cleanup no longer trusts that sentinel -- it bins the obvious
    boilerplate (ad marker / dateline / tip-line) and ships the surviving body."""

    database.run_migrations(env)
    article = (
        "REG AD\n\nPublished Thu 2 Jul 2026 // 08:00 UTC\n\n"
        "Share it with us at tips@example.com. Anonymity is available upon request.\n\n"
        + "The red teamers shoveled snow to gain physical access to the building. " * 8
    )
    monkeypatch.setattr(pipeline.chunker, "pack_paragraphs", lambda _md, _n: [article])

    async def _no_article(_system, _user, _settings, **_kwargs):
        return "NO_ARTICLE_CONTENT"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _no_article)
    cleaned = await pipeline._stage_cleanup("job", article, get_settings())
    assert "shoveled snow" in cleaned  # body shipped
    assert "REG AD" not in cleaned  # cruft binned
    assert "tips@example.com" not in cleaned
    assert "UTC" not in cleaned


async def test_cleanup_fails_when_page_is_only_boilerplate(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cruft-only page: the deterministic strip removes the subscribe/nav lines,
    leaving nothing above the floor -- so it still fails rather than narrate chrome."""

    database.run_migrations(env)
    boilerplate = "Sign up for our newsletter to get the latest updates delivered weekly.\n\n" * 6
    monkeypatch.setattr(pipeline.chunker, "pack_paragraphs", lambda _md, _n: [boilerplate])

    async def _no_article(_system, _user, _settings, **_kwargs):
        return "NO_ARTICLE_CONTENT"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _no_article)
    with pytest.raises(pipeline.CleanupTooShortError):
        await pipeline._stage_cleanup("job", boilerplate, get_settings())


async def test_cleanup_fallback_strips_markdown_links(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deterministic fallback strips inline links/images so the narrator reads the
    link label, not the URL."""

    database.run_migrations(env)
    article = "See the [official report](https://example.com/report.pdf) for details. " * 6
    monkeypatch.setattr(pipeline.chunker, "pack_paragraphs", lambda _md, _n: [article])

    async def _deflect(_system, _user, _settings, **_kwargs):
        return "Interesting piece! Want a summary?"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _deflect)
    cleaned = await pipeline._stage_cleanup("job", article, get_settings())
    assert "official report" in cleaned
    assert "https://example.com" not in cleaned
    assert "](" not in cleaned


async def test_cleanup_still_fails_when_extraction_also_thin(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely empty/boilerplate page (extraction itself below the floor) still
    fails -- the fallback must not resurrect a page that had no article to begin with."""

    database.run_migrations(env)
    thin = "cookie consent banner"
    monkeypatch.setattr(pipeline.chunker, "pack_paragraphs", lambda _md, _n: [thin])

    async def _empty(_system, _user, _settings, **_kwargs):
        return "NO_ARTICLE_CONTENT"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _empty)
    with pytest.raises(pipeline.CleanupTooShortError):
        await pipeline._stage_cleanup("job", thin, get_settings())


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


def test_normalize_us_states_city_comma() -> None:
    assert pipeline._normalize_us_states("Chicago, IL") == "Chicago, Illinois"
    # Trailing sentence punctuation is preserved, not consumed.
    assert pipeline._normalize_us_states("Springfield, IL.") == "Springfield, Illinois."
    assert (
        pipeline._normalize_us_states("a stop in Austin, TX, then home")
        == "a stop in Austin, Texas, then home"
    )
    assert pipeline._normalize_us_states("Denver, CO is high") == "Denver, Colorado is high"


def test_normalize_us_states_zip_context() -> None:
    # A safe code before a ZIP is already handled by the city-comma form.
    assert pipeline._normalize_us_states("Chicago, IL 60601") == "Chicago, Illinois 60601"
    # Ambiguous codes (OK/OR/PA/MA/...) expand only inside a comma-anchored ZIP address.
    assert pipeline._normalize_us_states("Tulsa, OK 74103") == "Tulsa, Oklahoma 74103"
    assert pipeline._normalize_us_states("Portland, OR 97201") == "Portland, Oregon 97201"
    assert pipeline._normalize_us_states("Pittsburgh, PA 15201") == "Pittsburgh, Pennsylvania 15201"


def test_normalize_us_states_leaves_words_alone() -> None:
    assert pipeline._normalize_us_states("that's OK with me") == "that's OK with me"
    assert pipeline._normalize_us_states("Well, OK.") == "Well, OK."  # ambiguous, no ZIP
    assert pipeline._normalize_us_states("you can fly, OR drive") == "you can fly, OR drive"
    assert pipeline._normalize_us_states("the group, PA system") == "the group, PA system"
    assert pipeline._normalize_us_states("filed, MA in economics") == "filed, MA in economics"
    assert pipeline._normalize_us_states("cats or dogs") == "cats or dogs"  # lowercase
    assert pipeline._normalize_us_states("Acme Co. shipped") == "Acme Co. shipped"
    assert pipeline._normalize_us_states("your photo, ID required") == "your photo, ID required"
    # A bare code before a 5-digit quantity (no comma anchor) is left alone.
    assert pipeline._normalize_us_states("grew IN 50000 homes") == "grew IN 50000 homes"


def test_normalize_for_tts_strips_headings_and_expands_dates() -> None:
    # "Jan" expands to the full month name (no longer respelled phonetically).
    out = pipeline._normalize_for_tts("### News\nShipped Jan 15 2026.")
    assert out == "News\nShipped January 15 2026."


def test_normalize_for_tts_expands_state_in_context() -> None:
    assert pipeline._normalize_for_tts("Based in Chicago, IL.") == "Based in Chicago, Illinois."


def test_fire_webhook_includes_resolved_voice(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import episodes, webhooks

    database.run_migrations(env)
    monkeypatch.setenv("WEBHOOK_URL", "https://hook.test/x")
    get_settings.cache_clear()
    captured: dict = {}
    monkeypatch.setattr(webhooks, "fire", lambda _s, payload: captured.update(payload))
    with database.connection(env) as conn:
        res = jobs.create_job(conn, "https://example.test/a", voice_id="3")
        episodes.upsert(
            conn,
            id=res.job.episode_id,
            job_id=res.job.id,
            original_url="https://example.test/a",
            title="A",
            author="x",
            audio_path=None,
            artwork_path=None,
            transcript_vtt=None,
            duration_secs=10,
            voice_label="Morgan",
        )
        conn.commit()
    pipeline._fire_webhook("episode.processed", res.job.id, get_settings())
    assert captured["voice"] == "Morgan"
    assert captured["title"] == "A"


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
    """Cleanup normalizes 'Feb 3' -> 'February 3'; month names are no longer respelled,
    so they reach the engine as plain full month names."""

    database.run_migrations(env)  # no user dict stored
    normalized = pipeline._normalize_for_tts("Posted Jan 15 2026 and Feb 3 2025.")
    out = asyncio.run(pipeline._apply_corrections(normalized, get_settings()))
    assert "January 15 2026" in out
    assert "February 3 2025" in out


# --- audio-QA: per-chunk regeneration on bad audio -------------------------


def _write_drone_wav(path: Path) -> None:
    import numpy as np
    import soundfile as sf

    t = np.arange(int(24000 * 1.0)) / 24000
    sf.write(str(path), (0.5 * np.sin(2 * np.pi * 440 * t)).astype("float32"), 24000, subtype="PCM_16")


def _write_speechlike_wav(path: Path, carrier: float = 180.0) -> None:
    import numpy as np
    import soundfile as sf

    t = np.arange(int(24000 * 2.0)) / 24000
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 4 * t)
    env[(t > 0.7) & (t < 0.95)] = 0.0
    sf.write(
        str(path),
        (0.6 * np.sin(2 * np.pi * carrier * t) * env).astype("float32"),
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

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        calls["n"] += 1
        seeds.append(seed)
        _write_drone_wav(wav) if calls["n"] == 1 else _write_speechlike_wav(wav)
        return tts.GenerateResult(wav_path=str(wav), duration_secs=1.0, sample_rate=24000)

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)
    job = _seed_job(env)
    result, _attempts = await pipeline._generate_chunk_quality_checked(
        job, "two words here now", 0, get_settings()
    )
    assert calls["n"] == 2  # one bad read, one regeneration that recovered
    assert result.wav_path == str(wav)
    # Attempt 0 passes no override (tts.generation_params applies the configured
    # CHATTERBOX_SEED baseline); the regen sends a distinct seed so Chatterbox
    # produces different audio.
    assert seeds[0] is None
    assert isinstance(seeds[1], int) and seeds[1] != 0


async def test_chunk_quality_check_passes_first_try_returns_one_attempt(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database.run_migrations(env)
    from app.services import tts

    wav = tmp_path / "ep_chunk_0.wav"

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        _write_speechlike_wav(wav)  # good on the first take, no regen needed
        return tts.GenerateResult(wav_path=str(wav), duration_secs=2.0, sample_rate=24000)

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)
    job = _seed_job(env)
    result, attempts = await pipeline._generate_chunk_quality_checked(
        job, "two words here now", 0, get_settings()
    )
    assert attempts == 1
    assert result.wav_path == str(wav)


async def test_chunk_quality_check_keeps_last_after_max_regen(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database.run_migrations(env)
    from app.services import tts

    wav = tmp_path / "ep_chunk_0.wav"
    calls = {"n": 0}

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        calls["n"] += 1
        _write_drone_wav(wav)  # always bad
        return tts.GenerateResult(wav_path=str(wav), duration_secs=1.0, sample_rate=24000)

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)
    job = _seed_job(env)
    result, _attempts = await pipeline._generate_chunk_quality_checked(
        job, "two words here now", 0, get_settings()
    )
    # 1 baseline + MAX_REGEN extra attempts; job is not failed (a result returns).
    assert calls["n"] == get_settings().AUDIO_ANALYSIS_MAX_REGEN + 1
    assert result.wav_path == str(wav)


async def test_chunk_pitch_drift_regenerates(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database.run_migrations(env)
    from app.services import audio_analysis, tts

    wav = tmp_path / "ep_chunk_0.wav"
    calls = {"n": 0}

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        calls["n"] += 1
        # First take drifts nearly 5 semitones off the reference; the regen lands on it.
        _write_speechlike_wav(wav, carrier=240.0 if calls["n"] == 1 else 180.0)
        return tts.GenerateResult(wav_path=str(wav), duration_secs=2.0, sample_rate=24000)

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)
    settings = get_settings()
    tracker = audio_analysis.PitchTracker(settings)
    for _ in range(settings.AUDIO_ANALYSIS_F0_WARMUP_CHUNKS):
        tracker.accept(180.0)
    job = _seed_job(env)
    result, _attempts = await pipeline._generate_chunk_quality_checked(
        job, "two words here now", 0, settings, pitch_tracker=tracker
    )
    assert calls["n"] == 2
    assert result.wav_path == str(wav)


async def test_chunk_pitch_drift_keeps_closest_attempt(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When every attempt drifts, the kept take is the one closest to the
    reference pitch, not the last one."""

    database.run_migrations(env)
    from app.services import audio_analysis, tts

    wav = tmp_path / "ep_chunk_0.wav"
    calls = {"n": 0}
    carriers = [240.0, 340.0, 300.0]  # deviations vs 180 Hz: ~5.0, ~11.0, ~8.8 st

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        _write_speechlike_wav(wav, carrier=carriers[calls["n"]])
        calls["n"] += 1
        return tts.GenerateResult(wav_path=str(wav), duration_secs=2.0, sample_rate=24000)

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)
    settings = get_settings()
    tracker = audio_analysis.PitchTracker(settings)
    for _ in range(settings.AUDIO_ANALYSIS_F0_WARMUP_CHUNKS):
        tracker.accept(180.0)
    job = _seed_job(env)
    result, _attempts = await pipeline._generate_chunk_quality_checked(
        job, "two words here now", 0, settings, pitch_tracker=tracker
    )
    assert calls["n"] == settings.AUDIO_ANALYSIS_MAX_REGEN + 1
    kept = audio_analysis.analyze_wav_path(result.wav_path, 4, settings)
    assert 220.0 <= kept.metrics.median_f0_hz <= 260.0


async def test_chunk_pitch_gate_inactive_during_warmup(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database.run_migrations(env)
    from app.services import audio_analysis, tts

    wav = tmp_path / "ep_chunk_0.wav"
    calls = {"n": 0}

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        calls["n"] += 1
        _write_speechlike_wav(wav, carrier=240.0)
        return tts.GenerateResult(wav_path=str(wav), duration_secs=2.0, sample_rate=24000)

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)
    settings = get_settings()
    tracker = audio_analysis.PitchTracker(settings)  # empty: warmup not satisfied
    job = _seed_job(env)
    await pipeline._generate_chunk_quality_checked(
        job, "two words here now", 0, settings, pitch_tracker=tracker
    )
    assert calls["n"] == 1


async def test_chunk_quality_check_disabled_calls_once(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AUDIO_ANALYSIS_ENABLED", "false")
    get_settings.cache_clear()
    database.run_migrations(env)
    from app.services import tts

    wav = tmp_path / "ep_chunk_0.wav"
    calls = {"n": 0}

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
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
        text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw
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
    result, _attempts = await pipeline._generate_chunk_quality_checked(job, text, 0, get_settings())
    assert calls["n"] == 2  # diverged once, matched on regen
    assert all(verifies)  # verify flag sent on every attempt
    assert result.transcript == text


async def test_chunk_asr_divergent_run_regenerates_despite_low_global(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Localized garbage: 10 of 30 words babble. Global divergence stays under
    # WHISPER_DIVERGENCE_THRESHOLD (the old blind spot) but the contiguous run
    # exceeds WHISPER_MAX_DIVERGENT_RUN, so the chunk must regenerate.
    monkeypatch.setenv("AUDIO_ANALYSIS_ENABLED", "false")
    monkeypatch.setenv("WHISPER_VERIFY_ENABLED", "true")
    get_settings.cache_clear()
    database.run_migrations(env)
    from app.services import asr_verify, tts

    text = " ".join(f"tok{i}" for i in range(30))
    garbled = [f"tok{i}" for i in range(30)]
    garbled[10:20] = [f"garble{i}" for i in range(10)]
    bad_transcript = " ".join(garbled)
    settings = get_settings()
    assert asr_verify.analyze(text, bad_transcript)[0] <= settings.WHISPER_DIVERGENCE_THRESHOLD

    calls = {"n": 0}

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        calls["n"] += 1
        return tts.GenerateResult(
            wav_path=str(tmp_path / "ep_chunk_0.wav"),
            duration_secs=1.0,
            sample_rate=24000,
            transcript=bad_transcript if calls["n"] == 1 else text,
        )

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)
    job = _seed_job(env)
    result, _attempts = await pipeline._generate_chunk_quality_checked(job, text, 0, settings)
    assert calls["n"] == 2  # run check flagged attempt 1, regen matched
    assert result.transcript == text


async def test_chunk_asr_short_chunk_regenerates_on_gross_mismatch(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AUDIO_ANALYSIS_ENABLED", "false")
    monkeypatch.setenv("WHISPER_VERIFY_ENABLED", "true")
    get_settings.cache_clear()
    database.run_migrations(env)
    from app.services import tts

    text = "three short words"
    calls = {"n": 0}
    verifies: list[bool] = []

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        calls["n"] += 1
        verifies.append(verify)
        return tts.GenerateResult(
            wav_path=str(tmp_path / "ep_chunk_0.wav"),
            duration_secs=1.0,
            sample_rate=24000,
            transcript=(
                "totally unrelated garbage instead" if calls["n"] == 1 else text
            ),
        )

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)
    job = _seed_job(env)
    # Short chunks (< WHISPER_VERIFY_MIN_WORDS) are still transcribed and fail
    # on gross mismatch -- a fully wrong 3-word chunk must not slip through.
    result, _attempts = await pipeline._generate_chunk_quality_checked(
        job, text, 0, get_settings()
    )
    assert calls["n"] == 2
    assert all(verifies)
    assert result.transcript == text


async def test_chunk_asr_short_chunk_tolerates_mild_divergence(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AUDIO_ANALYSIS_ENABLED", "false")
    monkeypatch.setenv("WHISPER_VERIFY_ENABLED", "true")
    get_settings.cache_clear()
    database.run_migrations(env)
    from app.services import tts

    calls = {"n": 0}

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        calls["n"] += 1
        return tts.GenerateResult(
            wav_path=str(tmp_path / "ep_chunk_0.wav"),
            duration_secs=1.0,
            sample_rate=24000,
            # Two of three proper nouns misheard (divergence ~0.67): whisper
            # mishears names deterministically on fine audio, so this must not
            # trigger a regen -- it would burn the full budget every episode.
            transcript="by Edscher Dykstra",
        )

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)
    job = _seed_job(env)
    await pipeline._generate_chunk_quality_checked(
        job, "by Edsger Dijkstra", 0, get_settings()
    )
    assert calls["n"] == 1


async def test_chunk_asr_acronym_heavy_chunk_gates_on_normalized_words(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 13 raw words but only 3 after acronym collapse: the short-chunk gate must
    # use the comparison word list, or ASR spelling of acronyms ("four tran")
    # hits the tuned 0.35 threshold and regenerates correct audio forever.
    monkeypatch.setenv("AUDIO_ANALYSIS_ENABLED", "false")
    monkeypatch.setenv("WHISPER_VERIFY_ENABLED", "true")
    get_settings.cache_clear()
    database.run_migrations(env)
    from app.services import tts

    calls = {"n": 0}

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        calls["n"] += 1
        return tts.GenerateResult(
            wav_path=str(tmp_path / "ep_chunk_0.wav"),
            duration_secs=1.0,
            sample_rate=24000,
            transcript="four tran and cobalt",
        )

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)
    job = _seed_job(env)
    await pipeline._generate_chunk_quality_checked(
        job, "F O R T R A N and C O B O L", 0, get_settings()
    )
    assert calls["n"] == 1


async def test_chunk_asr_missing_transcript_logged_and_passes(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Whisper enabled on the backend but the wrapper returned no transcript
    # (model down, per-chunk ASR error): the chunk passes as before, but the
    # silent skip must now be visible in the logs.
    monkeypatch.setenv("AUDIO_ANALYSIS_ENABLED", "false")
    monkeypatch.setenv("WHISPER_VERIFY_ENABLED", "true")
    get_settings.cache_clear()
    database.run_migrations(env)
    from app.services import tts

    calls = {"n": 0}

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        calls["n"] += 1
        return tts.GenerateResult(
            wav_path=str(tmp_path / "ep_chunk_0.wav"),
            duration_secs=1.0,
            sample_rate=24000,
            transcript=None,
        )

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)
    job = _seed_job(env)
    with caplog.at_level(logging.WARNING, logger="app.services.pipeline"):
        await pipeline._generate_chunk_quality_checked(
            job, "this chunk has clearly more than eight spoken words in it", 0, get_settings()
        )
    assert calls["n"] == 1
    assert any(
        getattr(r, "event", None) == "asr_transcript_missing" for r in caplog.records
    )


async def test_tts_cache_hit_skips_synthesis_on_second_call(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Wrapper-backend caching needs a resolvable model + voice fingerprint;
    # the default TTS_MODEL="" deliberately skips caching (see
    # pipeline._tts_cache_identity), so opt in explicitly for this test.
    monkeypatch.setenv("TTS_MODEL", "chatterbox-turbo")
    get_settings.cache_clear()
    database.run_migrations(env)
    from app.services import tts

    calls = {"n": 0}

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        calls["n"] += 1
        wav = tmp_path / f"synth_{calls['n']}.wav"
        _write_speechlike_wav(wav)  # clean take: QA passes on the first try
        return tts.GenerateResult(wav_path=str(wav), duration_secs=2.0, sample_rate=24000)

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)

    settings = get_settings()
    job1 = _seed_job(env, url="https://example.test/article-one")
    result1, attempts1 = await pipeline._generate_chunk_quality_checked(
        job1, "two words here now", 0, settings, slot=1
    )
    assert calls["n"] == 1
    assert attempts1 == 1

    # A different job/episode, same text + settings: must hit the cache and
    # never call the fake again.
    job2 = _seed_job(env, url="https://example.test/article-two")
    result2, attempts2 = await pipeline._generate_chunk_quality_checked(
        job2, "two words here now", 0, settings, slot=1
    )
    assert calls["n"] == 1
    assert attempts2 == 1
    assert result2.wav_path != result1.wav_path
    assert Path(result2.wav_path).is_file()
    assert Path(result2.wav_path).read_bytes() == Path(result1.wav_path).read_bytes()
    assert result2.duration_secs == 2.0
    assert result2.sample_rate == 24000
    # Cache-hit reconstruction leaves the wrapper-diagnostics fields unknown.
    assert result2.inference_ms is None
    assert result2.verify_ms is None
    assert result2.rss_mb is None


async def test_tts_cache_disabled_always_resynthesizes(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TTS_MODEL", "chatterbox-turbo")
    monkeypatch.setenv("TTS_CHUNK_CACHE_ENABLED", "false")
    get_settings.cache_clear()
    database.run_migrations(env)
    from app.services import tts

    calls = {"n": 0}

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        calls["n"] += 1
        wav = tmp_path / f"synth_{calls['n']}.wav"
        _write_speechlike_wav(wav)
        return tts.GenerateResult(wav_path=str(wav), duration_secs=2.0, sample_rate=24000)

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)

    settings = get_settings()
    job1 = _seed_job(env, url="https://example.test/article-three")
    await pipeline._generate_chunk_quality_checked(
        job1, "two words here now", 0, settings, slot=1
    )
    job2 = _seed_job(env, url="https://example.test/article-four")
    await pipeline._generate_chunk_quality_checked(
        job2, "two words here now", 0, settings, slot=1
    )
    assert calls["n"] == 2  # cache disabled: no reuse


async def test_tts_cache_skipped_when_model_select_unconfirmed(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed /select-model leaves the wrapper on an unknown model. Caching
    this job's chunks under settings.TTS_MODEL anyway would let a later job
    (with a working select) get served audio actually produced by a
    different model -- so model_confirmed=False must disable the cache for
    both read and write, not just skip the store."""
    monkeypatch.setenv("TTS_MODEL", "chatterbox-turbo")
    get_settings.cache_clear()
    database.run_migrations(env)
    from app.services import tts

    calls = {"n": 0}

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        calls["n"] += 1
        wav = tmp_path / f"synth_{calls['n']}.wav"
        _write_speechlike_wav(wav)
        return tts.GenerateResult(wav_path=str(wav), duration_secs=2.0, sample_rate=24000)

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)

    settings = get_settings()
    job1 = _seed_job(env, url="https://example.test/article-five")
    await pipeline._generate_chunk_quality_checked(
        job1, "two words here now", 0, settings, slot=1, model_confirmed=False
    )
    job2 = _seed_job(env, url="https://example.test/article-six")
    await pipeline._generate_chunk_quality_checked(
        job2, "two words here now", 0, settings, slot=1, model_confirmed=False
    )
    assert calls["n"] == 2  # unconfirmed model: never stored, never hit


async def test_tts_cache_store_failure_does_not_fail_chunk(
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The cache must never fail a chunk: a broken tts_cache.store (disk
    full, permission error, ...) after an otherwise-clean synthesis still
    has to return the chunk that was actually produced."""
    monkeypatch.setenv("TTS_MODEL", "chatterbox-turbo")
    get_settings.cache_clear()
    database.run_migrations(env)
    from app.services import tts, tts_cache

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        wav = tmp_path / "synth.wav"
        _write_speechlike_wav(wav)
        return tts.GenerateResult(wav_path=str(wav), duration_secs=2.0, sample_rate=24000)

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)

    def _boom_store(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(tts_cache, "store", _boom_store)

    settings = get_settings()
    job = _seed_job(env, url="https://example.test/article-store-fail")
    with caplog.at_level(logging.WARNING, logger="app.services.pipeline"):
        result, attempts = await pipeline._generate_chunk_quality_checked(
            job, "two words here now", 0, settings, slot=1
        )
    assert attempts == 1
    assert Path(result.wav_path).is_file()
    assert any(getattr(r, "event", "") == "tts_cache_error" for r in caplog.records)


async def test_tts_cache_lookup_failure_falls_through_to_synthesis(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A broken tts_cache.lookup (corrupt cache dir, permission error, ...)
    must not block the chunk -- it falls through to normal synthesis."""
    monkeypatch.setenv("TTS_MODEL", "chatterbox-turbo")
    get_settings.cache_clear()
    database.run_migrations(env)
    from app.services import tts, tts_cache

    calls = {"n": 0}

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        calls["n"] += 1
        wav = tmp_path / f"synth_{calls['n']}.wav"
        _write_speechlike_wav(wav)
        return tts.GenerateResult(wav_path=str(wav), duration_secs=2.0, sample_rate=24000)

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _fake_tts)

    def _boom_lookup(*_a, **_kw):
        raise OSError("cache dir unreadable")

    monkeypatch.setattr(tts_cache, "lookup", _boom_lookup)

    settings = get_settings()
    job = _seed_job(env, url="https://example.test/article-lookup-fail")
    result, attempts = await pipeline._generate_chunk_quality_checked(
        job, "two words here now", 0, settings, slot=1
    )
    assert calls["n"] == 1  # fell through to synthesis despite the lookup failure
    assert attempts == 1
    assert Path(result.wav_path).is_file()


async def test_pipeline_cancelled_job_ends_cancelled_not_failed(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)
    _stub_full_chain(monkeypatch)
    job = _seed_job(env)
    # Operator cancels: the row is flipped while the worker holds the job.
    conn = database.connect(database.db_path(env))
    try:
        jobs.mark_cancelled(conn, job.id)
        conn.commit()
    finally:
        conn.close()

    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "cancelled"  # not 'failed' and not 'done'
    assert after.error == "cancelled by user"


async def test_stage_tts_carries_the_job_voice_on_every_chunk(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every chunk carries the job's slot so the wrapper re-selects inside the same
    lock as inference and a concurrent audition can't switch voices mid-episode. The
    standalone select happens once, up front."""

    database.run_migrations(env)
    from app.services import tts as tts_mod

    selected: list[int] = []
    per_chunk_slots: list[int | None] = []

    async def _fake_select(_settings, slot):
        selected.append(slot)

    async def _fake_gen(
        _job, _text, index, _settings, slot=None, pitch_tracker=None, model_confirmed=True
    ):
        per_chunk_slots.append(slot)
        return (
            tts_mod.GenerateResult(
                wav_path=f"/tmp/c{index}.wav", duration_secs=1.0, sample_rate=24000
            ),
            1,
        )

    monkeypatch.setattr(tts_mod, "select_voice", _fake_select)
    monkeypatch.setattr(pipeline, "_generate_chunk_quality_checked", _fake_gen)
    monkeypatch.setattr(pipeline, "_set_progress", lambda *a, **k: None)

    job = jobs.Job(
        id="vjob",
        url="u",
        episode_id="e",
        status="processing",
        stage="tts",
        error=None,
        created_at="t",
        updated_at="t",
        voice_id="2",
    )
    await pipeline._stage_tts(job, ["one.", "two.", "three."], get_settings())
    assert selected == [2]
    assert per_chunk_slots == [2, 2, 2]


def test_regen_params_leaves_the_baseline_take_untouched() -> None:
    """Attempt 0 must be exactly what the operator configured, so escalation
    can't silently change the audio of a chunk that was never in trouble."""

    assert pipeline._regen_params(get_settings(), 0) == (None, None)


def test_regen_params_shrink_the_window_and_stiffen_the_penalty_each_attempt() -> None:
    s = SimpleNamespace(
        CHATTERBOX_MAX_CHARS=500,
        CHATTERBOX_REPETITION_PENALTY=1.2,
        AUDIO_ANALYSIS_REGEN_CHARS_FACTOR=0.6,
        AUDIO_ANALYSIS_REGEN_MIN_CHARS=150,
        AUDIO_ANALYSIS_REGEN_PENALTY_STEP=0.15,
    )
    assert pipeline._regen_params(s, 1) == (300, pytest.approx(1.35))
    assert pipeline._regen_params(s, 2) == (180, pytest.approx(1.5))
    # Attempt 3 would scale to 108 chars; the operator floor holds it at 150.
    assert pipeline._regen_params(s, 3) == (150, pytest.approx(1.65))


def test_regen_params_stay_inside_the_wrappers_request_bounds() -> None:
    """CHATTERBOX_MAX_CHARS carries no pydantic constraint and the penalty step
    is operator-set, so extreme settings must still produce a legal request --
    a 422 from the wrapper would turn a degraded chunk into a failed job."""

    s = SimpleNamespace(
        CHATTERBOX_MAX_CHARS=99999,
        CHATTERBOX_REPETITION_PENALTY=1.9,
        AUDIO_ANALYSIS_REGEN_CHARS_FACTOR=1.0,
        AUDIO_ANALYSIS_REGEN_MIN_CHARS=100,
        AUDIO_ANALYSIS_REGEN_PENALTY_STEP=1.0,
    )
    max_chars, penalty = pipeline._regen_params(s, 1)
    assert max_chars == 2000
    assert penalty == 2.0

    tiny = SimpleNamespace(
        CHATTERBOX_MAX_CHARS=10,
        CHATTERBOX_REPETITION_PENALTY=1.0,
        AUDIO_ANALYSIS_REGEN_CHARS_FACTOR=0.1,
        AUDIO_ANALYSIS_REGEN_MIN_CHARS=100,
        AUDIO_ANALYSIS_REGEN_PENALTY_STEP=0.0,
    )
    assert pipeline._regen_params(tiny, 1) == (100, 1.0)


async def test_narration_is_on_disk_when_the_job_dies_in_tts(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """episodes.cleaned_text is only written at finalize, so a TTS failure used
    to leave nothing to diagnose. The normalize stage now dumps the narration
    next to the media so the text that broke synthesis survives the failure."""

    from app.core.paths import media_dir

    database.run_migrations(env)
    _stub_full_chain(monkeypatch)

    from app.services import tts

    async def _boom_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        raise tts.TTSRequestError("wrapper said no")

    monkeypatch.setattr(tts, "generate_chunk_with_retry", _boom_tts)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "failed"
    assert after.stage == "tts"

    narration = media_dir(get_settings()) / f"{job.episode_id}.narration.txt"
    assert narration.exists()
    assert "cleaned narration" in narration.read_text(encoding="utf-8")


async def test_narration_write_failure_does_not_fail_the_job(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forensics are never worth losing an episode over."""

    database.run_migrations(env)
    _stub_full_chain(monkeypatch)

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline, "write_bytes_atomic", _boom)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())

    after = _job_after(env, job.id)
    assert after.status == "done"
    assert after.error is None


# --- 2e: chunk the cleaned text, normalize per chunk ------------------------


async def test_stage_normalize_returns_per_chunk_narrations(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)
    seen: dict = {}
    monkeypatch.setattr(pipeline, "_llm_with_retry", _echo_window(seen))
    job = _seed_job(env)
    chunks = ["I ran a SQL query today.", "A plain sentence follows here."]
    out = await pipeline._stage_normalize(job, chunks, get_settings())
    assert isinstance(out, list) and len(out) == 2
    assert "sequel" in out[0]  # backstop respelled the term chunk-locally
    assert out[1] == "A plain sentence follows here."


async def test_pronounce_skips_llm_when_no_reference_terms_present(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)
    calls = {"n": 0}

    async def _fake(_system, _user, _settings, **_kwargs):
        calls["n"] += 1
        return "<<<AUDICLE_BEGIN>>>\nwhatever\n<<<AUDICLE_END>>>"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    chunk = "Nothing here matches the reference vocabulary in any way."
    out = await pipeline._pronounce_chunks_with_llm("job", [chunk], get_settings())
    assert calls["n"] == 0
    assert out == [chunk]


async def test_pronounce_reference_filtered_to_present_terms(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)
    prompts: list[str] = []

    async def _fake(system, user, _settings, **_kwargs):
        prompts.append(system)
        body = user.split("<text>\n", 1)[1].rsplit("\n</text>", 1)[0]
        return f"<<<AUDICLE_BEGIN>>>\n{body}\n<<<AUDICLE_END>>>"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    await pipeline._pronounce_chunks_with_llm("job", ["I ran a SQL query."], get_settings())
    assert len(prompts) == 1
    assert "SQL" in prompts[0]
    assert "Kubernetes" not in prompts[0]  # absent from the chunk, filtered out


async def test_run_stages_vtt_keeps_original_text_while_tts_speaks_narration(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2e acceptance: the served VTT reads the cleaned text (SQL) while the
    audio path receives the respelled narration (sequel)."""

    database.run_migrations(env)
    _stub_full_chain(monkeypatch)

    cleaned = (
        "The team wrote a SQL query to answer the question. "
        "It ran quickly on the analytics cluster. "
        "Results were shared with the whole company for discussion. "
    ) * 8

    async def _fake_llm(system, user, _settings, **_kwargs):
        if "PRONUNCIATION REFERENCE" in system:
            body = user.split("<text>\n", 1)[1].rsplit("\n</text>", 1)[0]
            return f"<<<AUDICLE_BEGIN>>>\n{body}\n<<<AUDICLE_END>>>"
        return cleaned

    monkeypatch.setattr(llm, "generate", _fake_llm)

    from app.services import tts as tts_module

    spoken: list[str] = []

    async def _fake_tts(text, episode_id, chunk_index, settings, seed=None, verify=False, **_kw):
        spoken.append(text)
        return tts_module.GenerateResult(
            wav_path=f"/tmp/{episode_id}_chunk_{chunk_index}.wav",
            duration_secs=1.0,
            sample_rate=24000,
        )

    monkeypatch.setattr(tts_module, "generate_chunk_with_retry", _fake_tts)

    job = _seed_job(env)
    await pipeline.process_job(job, get_settings())
    after = _job_after(env, job.id)
    assert after.status == "done"

    from app.services import episodes as episodes_service

    conn = database.connect(database.db_path(env))
    try:
        row = episodes_service.get_by_id(conn, job.episode_id)
        stored_text = episodes_service.get_cleaned_text(conn, job.episode_id)
    finally:
        conn.close()
    assert row is not None and row.transcript_vtt
    assert "SQL" in row.transcript_vtt
    assert "sequel" not in row.transcript_vtt
    assert any("sequel" in text for text in spoken)
    assert not any("SQL" in text for text in spoken)
    # The stored cleaned text is the readable original, not the narration.
    assert stored_text and "sequel" not in stored_text


# --- 3a: intro line ---------------------------------------------------------


def test_intro_line_with_title_and_author(env: Path) -> None:
    line = pipeline._intro_read_line(
        {"title": "The Big Story", "author": "Jane Doe"}, get_settings()
    )
    assert line == "The Big Story. By Jane Doe."


def test_intro_line_omits_author_when_missing(env: Path) -> None:
    assert pipeline._intro_read_line({"title": "The Big Story"}, get_settings()) == (
        "The Big Story."
    )


def test_intro_line_keeps_terminal_punctuation(env: Path) -> None:
    line = pipeline._intro_read_line({"title": "Is AI Over?"}, get_settings())
    assert line == "Is AI Over?"


def test_intro_line_disabled_or_untitled_returns_none(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert pipeline._intro_read_line({}, get_settings()) is None
    monkeypatch.setenv("INTRO_READ_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert (
            pipeline._intro_read_line({"title": "The Big Story"}, get_settings()) is None
        )
    finally:
        get_settings.cache_clear()


async def test_run_stages_prepends_intro_before_chunking(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    from app.services import episodes as episodes_service

    conn = database.connect(database.db_path(env))
    try:
        row = episodes_service.get_by_id(conn, job.episode_id)
        stored_text = episodes_service.get_cleaned_text(conn, job.episode_id)
    finally:
        conn.close()
    assert stored_text and stored_text.startswith("Test Article. By Test Author.")
    # The intro flows into the first transcript cue like any other chunk text.
    assert row is not None and "Test Article. By Test Author." in row.transcript_vtt


# --- 3b: chapters stage -----------------------------------------------------


async def test_stage_chapters_skipped_below_min_duration(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)
    calls = {"n": 0}

    async def _fake(_s, _u, _settings, **_k):
        calls["n"] += 1
        return "0 | Nope"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    out = await pipeline._stage_chapters(
        ["one", "two"], [30.0, 30.0], 60.0, Path("/tmp/x.mp3"), get_settings()
    )
    assert out is None
    assert calls["n"] == 0


async def test_stage_chapters_generates_document_with_exact_timestamps(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)

    async def _fake(_system, user, _settings, **_k):
        assert "[00:00]" in user  # timestamped transcript sent
        return "00:00 Opening remarks\n08:23 The second act"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    embedded = {}

    def _spy_embed(mp3_path, pairs, total):
        embedded["pairs"] = pairs
        embedded["total"] = total

    monkeypatch.setattr(pipeline.audio, "embed_chapters", _spy_embed)
    import json as json_mod

    out = await pipeline._stage_chapters(
        ["a", "b", "c"], [300.0, 200.0, 200.0], 700.0, Path("/tmp/x.mp3"), get_settings()
    )
    assert out is not None
    doc = json_mod.loads(out)
    assert doc["chapters"][0] == {"startTime": 0, "title": "Opening remarks"}
    # 08:23 is the timestamp the model returned, echoed back as whole seconds.
    assert doc["chapters"][1] == {"startTime": 503, "title": "The second act"}
    assert embedded["pairs"][0] == (0.0, "Opening remarks")
    assert embedded["total"] == 700.0


async def test_stage_chapters_llm_failure_returns_none(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)

    async def _boom(_s, _u, _settings, **_k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(pipeline, "_llm_with_retry", _boom)
    out = await pipeline._stage_chapters(
        ["one", "two"], [400.0, 400.0], 800.0, Path("/tmp/x.mp3"), get_settings()
    )
    assert out is None


# --- section 5: model applied per job ----------------------------------------


async def test_stage_tts_applies_configured_model(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)
    from app.services import tts as tts_mod

    selected_models: list[str] = []

    async def _fake_select_model(_settings, model):
        selected_models.append(model)

    async def _fake_gen(
        _job, _text, index, _settings, slot=None, pitch_tracker=None, model_confirmed=True
    ):
        return (
            tts_mod.GenerateResult(
                wav_path=f"/tmp/c{index}.wav", duration_secs=1.0, sample_rate=24000
            ),
            1,
        )

    async def _fake_select_voice(_settings, _slot):
        pass

    monkeypatch.setattr(tts_mod, "select_model_with_retry", _fake_select_model)
    monkeypatch.setattr(tts_mod, "select_voice_with_retry", _fake_select_voice)
    monkeypatch.setattr(pipeline, "_generate_chunk_quality_checked", _fake_gen)
    monkeypatch.setattr(pipeline, "_set_progress", lambda *a, **k: None)
    monkeypatch.setenv("TTS_MODEL", "chatterbox-multilingual")
    get_settings.cache_clear()
    try:
        job = _seed_job(env)
        await pipeline._stage_tts(job, ["one chunk"], get_settings())
    finally:
        get_settings.cache_clear()
    assert selected_models == ["chatterbox-multilingual"]


# --- LLM rate-limit backoff -------------------------------------------------


def test_retry_wait_honors_provider_retry_after() -> None:
    """A 429 carrying retry_after waits at least that long, so a 3-attempt
    budget doesn't burn through a 60 s window in 4 seconds."""

    from app.services import llm as llm_mod

    wait = pipeline._llm_retry_wait
    state = _retry_state(llm_mod.LLMRateLimitError("slow down", retry_after=60))
    assert wait(state) == pytest.approx(60.0)


def test_retry_wait_caps_absurd_retry_after() -> None:
    from app.services import llm as llm_mod

    state = _retry_state(llm_mod.LLMRateLimitError("slow down", retry_after=3600))
    assert pipeline._llm_retry_wait(state) == pytest.approx(pipeline._LLM_RETRY_MAX_WAIT)


def test_retry_wait_falls_back_to_exponential_without_hint() -> None:
    from app.services import llm as llm_mod

    state = _retry_state(llm_mod.LLMProviderError("boom"))
    assert 0 < pipeline._llm_retry_wait(state) <= pipeline._LLM_RETRY_MAX_WAIT


def _retry_state(exc: Exception):
    """Minimal tenacity RetryCallState carrying ``exc`` as the outcome."""

    from tenacity import Future, RetryCallState

    state = RetryCallState(retry_object=None, fn=None, args=(), kwargs={})
    state.attempt_number = 1
    future = Future(1)
    future.set_exception(exc)
    state.outcome = future
    return state


async def test_stage_chapters_survives_a_chunk_duration_desync(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chapters must never be what fails an episode: a desync is the transcript
    stage's error to raise, not this one's."""

    database.run_migrations(env)

    async def _fake(_system, _user, _settings, **_k):
        return "00:00 One\n05:00 Two"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    monkeypatch.setattr(pipeline.audio, "embed_chapters", lambda *a, **k: None)
    out = await pipeline._stage_chapters(
        ["a", "b", "c"], [400.0, 400.0], 800.0, Path("/tmp/x.mp3"), get_settings()
    )
    assert out is not None


# --- title echo: the body repeats its own headline (0.52.5) ----------------


def test_strips_a_leading_title_echo_before_the_intro() -> None:
    """Substack renders headline + subtitle, so the cleaned body opens with the
    same title the intro read announces. Note the apostrophes differ: the
    metadata title is curly, the body straight."""

    # \u2019 (curly) here vs a straight quote in the body: the real mismatch.
    title = "Zillow\u2019s CEO Just Fired 500 People Because He Says The Company is More Efficient Without Them"
    body = (
        "Zillow's CEO Just Fired 500 People Because He Says The Company is More Efficient Without Them\n\n"
        "Zillow just posted the best financial year in its two-decade history, then cut "
        "seven hundred people.\n\nOn Tuesday morning, August 4, 2026, just over five hundred "
        "people at Zillow Group learned they no longer had jobs."
    )
    out = pipeline._strip_title_echo(body, title)
    assert not out.startswith("Zillow's CEO Just Fired")
    assert out.startswith("Zillow just posted the best financial year")
    assert "learned they no longer had jobs" in out


def test_keeps_the_body_when_it_does_not_open_with_the_title() -> None:
    title = "A Completely Different Headline"
    body = "The article opens on a scene instead.\n\nThen it continues."
    assert pipeline._strip_title_echo(body, title) == body


def test_keeps_a_paragraph_that_merely_starts_like_the_title() -> None:
    """Only a heading-shaped echo is dropped; a real opening paragraph that
    happens to begin with the headline's words is content."""

    title = "Zillow Fired 500 People"
    body = (
        "Zillow Fired 500 People, and the timing tells you everything about how the "
        "company thinks about its own record year and what comes next for the market.\n\n"
        "More reporting follows."
    )
    assert pipeline._strip_title_echo(body, title) == body


def test_title_echo_tolerates_punctuation_and_case_drift() -> None:
    title = "The AI Explanation Nobody Confirms"
    body = "the ai explanation nobody confirms.\n\nReal content here."
    assert pipeline._strip_title_echo(body, title) == "Real content here."


def test_title_echo_no_title_is_a_noop() -> None:
    body = "Some article text."
    assert pipeline._strip_title_echo(body, None) == body
    assert pipeline._strip_title_echo(body, "") == body
