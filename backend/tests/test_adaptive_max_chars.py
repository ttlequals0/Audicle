from __future__ import annotations

from pathlib import Path

import pytest
from app.config import get_settings
from app.core import database
from app.services import jobs, pipeline


def _job(job_id: str, episode_id: str) -> jobs.Job:
    return jobs.Job(
        id=job_id,
        url="u",
        episode_id=episode_id,
        status="processing",
        stage="tts",
        error=None,
        created_at="t",
        updated_at="t",
        voice_id="2",
    )


def _wire_fake_gen(monkeypatch: pytest.MonkeyPatch, received: list[int | None]):
    from app.services import tts as tts_mod

    async def _fake_select(_settings, _slot):
        pass

    async def _fake_gen(
        _job,
        _text,
        index,
        _settings,
        slot=None,
        pitch_tracker=None,
        model_confirmed=True,
        base_max_chars=None,
    ):
        received.append(base_max_chars)
        attempts = 2 if index in (0, 1) else 1
        return (
            tts_mod.GenerateResult(
                wav_path=f"/tmp/c{index}.wav", duration_secs=1.0, sample_rate=24000
            ),
            attempts,
        )

    monkeypatch.setattr(tts_mod, "select_voice", _fake_select)
    monkeypatch.setattr(pipeline, "_generate_chunk_quality_checked", _fake_gen)
    monkeypatch.setattr(pipeline, "_set_progress", lambda *a, **k: None)


async def test_adaptive_max_chars_lowers_after_early_qa_failures(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two chunks inside the first _ADAPTIVE_WINDOW needing a regen (attempts > 1)
    lower max_chars for every chunk from that point on; the two chunks that
    triggered it, and everything before them, still got the operator's
    configured baseline (None)."""

    database.run_migrations(env)
    received: list[int | None] = []
    _wire_fake_gen(monkeypatch, received)

    settings = get_settings()
    chunks = [f"chunk {i}" for i in range(5)]
    await pipeline._stage_tts(_job("ajob", "e1"), chunks, settings)

    expected = int(
        pipeline._clamp(
            settings.CHATTERBOX_MAX_CHARS * pipeline._ADAPTIVE_FACTOR,
            pipeline._MAX_CHARS_BOUNDS,
        )
    )
    assert received == [None, None, expected, expected, expected]


async def test_adaptive_max_chars_disabled_never_overrides(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same early-failure pattern, but with the setting off: max_chars must stay
    at the operator's baseline (None) for the whole job."""

    database.run_migrations(env)
    received: list[int | None] = []
    _wire_fake_gen(monkeypatch, received)
    monkeypatch.setenv("TTS_ADAPTIVE_MAX_CHARS_ENABLED", "false")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        chunks = [f"chunk {i}" for i in range(5)]
        await pipeline._stage_tts(_job("ajob2", "e2"), chunks, settings)
    finally:
        get_settings.cache_clear()

    assert received == [None, None, None, None, None]
