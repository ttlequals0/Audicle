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

    async def _stub_resolve(_host: str) -> str:
        return "203.0.113.1"

    monkeypatch.setattr(_artwork, "_assert_public_host", _allow_all)
    monkeypatch.setattr(_artwork, "_resolve_public_host", _stub_resolve)


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


# --- corrections stage: seed + user merge ----------------------------------


def test_corrections_applies_seed_brand_phrase(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An applicable seed row (a brand phrase) is corrected even with no user
    dictionary present."""

    database.run_migrations(env)  # no user dict stored -> empty user corrections
    out = asyncio.run(
        pipeline._stage_corrections("I bought a Louis Vuitton bag.", get_settings())
    )
    assert "loo-ee vwee-TOHN" in out


def test_corrections_user_override_beats_seed(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user correction whose key also exists in the seed wins."""

    from app.services import corrections as corrections_service

    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        corrections_service.save_user_dict(conn, {"Louis Vuitton": "ELL VEE"})
    finally:
        conn.close()
    out = asyncio.run(pipeline._stage_corrections("My Louis Vuitton bag.", get_settings()))
    assert "ELL VEE" in out
    assert "loo-ee vwee-TOHN" not in out


def test_corrections_malformed_seed_falls_back_to_user_only(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed bundled seed CSV must not fail the job; user corrections
    still apply."""

    from app.services import corrections as corrections_service
    from app.services import seed_corrections

    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        corrections_service.save_user_dict(conn, {"widget": "wid jet"})
    finally:
        conn.close()
    bad_seed = tmp_path / "bad_seed.csv"
    bad_seed.write_text("wrong,columns\na,b\n", encoding="utf-8")
    monkeypatch.setattr(seed_corrections, "seed_path", lambda: bad_seed)
    out = asyncio.run(pipeline._stage_corrections("a widget here", get_settings()))
    assert "wid jet" in out


# --- cleanup: boilerplate-only window drop ---------------------------------


def test_is_empty_section_detects_sentinel_and_disclaimers() -> None:
    assert pipeline._is_empty_section("NO_ARTICLE_CONTENT")
    assert pipeline._is_empty_section("NO_ARTICLE_CONTENT.")
    assert pipeline._is_empty_section('"NO_ARTICLE_CONTENT"')
    assert pipeline._is_empty_section('"NO_ARTICLE_CONTENT".')
    # The two real disclaimers the model leaked in the incident.
    assert pipeline._is_empty_section(
        "There is no article content in what you provided. The entire text is "
        "website cookie-consent and privacy-policy boilerplate."
    )
    assert pipeline._is_empty_section(
        "If you paste the article body text, I can clean it for you."
    )


def test_is_empty_section_keeps_real_prose() -> None:
    # Normal narration is not dropped, even when it mentions "article".
    assert not pipeline._is_empty_section(
        "This article explains how the kernel boots in six phases. " * 5
    )
    assert not pipeline._is_empty_section(
        "The mayor announced a five hundred thousand dollar settlement today."
    )
    # A real column that opens "There is no article this week" must survive: it
    # hits the disclaimer opener but has no supplied-input signal.
    assert not pipeline._is_empty_section(
        "There is no article this week, so instead we round up the month's best reads."
    )


def test_is_empty_section_catches_sentinel_with_stray_prose() -> None:
    # Model adds stray text around the sentinel -> still dropped (containment).
    assert pipeline._is_empty_section("NO_ARTICLE_CONTENT\n\nSkip to main content")


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
    out = pipeline._normalize_for_tts("### News\nShipped Jan 15 2026.")
    assert out == "News\nShipped January 15 2026."


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


def test_normalize_numbers_leaves_ambiguous_and_glued_alone() -> None:
    # Bare integers (day/year), versions, IPs, and code-glued digits are
    # context-dependent -- left to the LLM prompt, untouched here.
    assert pipeline._normalize_numbers("In 2026 we saw 15 things") == "In 2026 we saw 15 things"
    assert pipeline._normalize_numbers("version 1.2.3 shipped") == "version 1.2.3 shipped"
    assert pipeline._normalize_numbers("ip 10.0.0.1 here") == "ip 10.0.0.1 here"
    assert pipeline._normalize_numbers("x86 and startup_32") == "x86 and startup_32"


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
    """Cleanup normalizes 'Feb 3' -> 'February 3'; the corrections stage then
    voices February via the seed pronunciation."""

    database.run_migrations(env)  # no user dict stored
    normalized = pipeline._normalize_for_tts("Posted Jan 15 2026 and Feb 3 2025.")
    out = asyncio.run(pipeline._stage_corrections(normalized, get_settings()))
    assert "January 15 2026" in out
    assert "FEB-roo-air-ee 3 2025" in out
