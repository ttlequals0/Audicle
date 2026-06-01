from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from config import Config
from engine import _XTTS_MAX_CHARS, InferenceBusyError, XTTSEngine, _split_for_xtts

_COND_ENV = ("XTTS_GPT_COND_LEN", "XTTS_GPT_COND_CHUNK_LEN", "XTTS_MAX_REF_LENGTH", "XTTS_SPEED")


def test_conditioning_defaults_match_xtts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unset -> XTTS-v2's own defaults, so prior behavior is reproduced.
    for name in _COND_ENV:
        monkeypatch.delenv(name, raising=False)
    cfg = Config.from_env()
    assert (cfg.gpt_cond_len, cfg.gpt_cond_chunk_len, cfg.max_ref_length, cfg.speed) == (6, 6, 30, 1.0)


def test_conditioning_overrides_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XTTS_GPT_COND_LEN", "30")
    monkeypatch.setenv("XTTS_GPT_COND_CHUNK_LEN", "12")
    monkeypatch.setenv("XTTS_MAX_REF_LENGTH", "60")
    monkeypatch.setenv("XTTS_SPEED", "0.95")
    cfg = Config.from_env()
    assert (cfg.gpt_cond_len, cfg.gpt_cond_chunk_len, cfg.max_ref_length, cfg.speed) == (30, 12, 60, 0.95)


def test_compute_embeddings_forwards_conditioning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XTTS_GPT_COND_LEN", "30")
    monkeypatch.setenv("XTTS_GPT_COND_CHUNK_LEN", "12")
    monkeypatch.setenv("XTTS_MAX_REF_LENGTH", "60")
    captured: dict = {}

    class _FakeInner:
        def get_conditioning_latents(self, **kwargs):
            captured.update(kwargs)
            return ("latent", "embed")

    class _FakeModel:
        class synthesizer:  # noqa: N801
            tts_model = _FakeInner()

    engine = XTTSEngine(Config.from_env())
    engine._model = _FakeModel()
    ref = tmp_path / "voice.wav"
    engine._compute_embeddings(ref)
    assert captured["audio_path"] == [str(ref)]
    assert captured["gpt_cond_len"] == 30
    assert captured["gpt_cond_chunk_len"] == 12
    assert captured["max_ref_length"] == 60
    assert engine.reference_loaded is True


def test_compute_embeddings_rejects_when_gpu_busy(tmp_path: Path) -> None:
    # /reload's embedding recompute is GPU work; it must not run while an
    # inference (e.g. an orphaned post-timeout thread) still holds the GPU lock.
    engine = XTTSEngine(Config.from_env())
    engine._model = object()
    assert engine._gpu_lock.acquire(blocking=False)
    try:
        with pytest.raises(InferenceBusyError):
            engine._compute_embeddings(tmp_path / "voice.wav")
    finally:
        engine._gpu_lock.release()


def test_infer_piece_forwards_speed_and_sampling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XTTS_SPEED", "0.9")
    captured: dict = {}

    class _FakeInner:
        def inference(self, **kwargs):
            captured.update(kwargs)
            return {"wav": np.zeros(8, dtype=np.float32)}

    class _FakeModel:
        class synthesizer:  # noqa: N801
            tts_model = _FakeInner()

    engine = XTTSEngine(Config.from_env())
    engine._model = _FakeModel()
    engine._gpt_cond_latent = "latent"
    engine._speaker_embedding = "embed"
    engine._infer_piece("hello there")
    assert captured["speed"] == 0.9
    assert captured["temperature"] == engine.config.temperature
    assert captured["text"] == "hello there"


def test_run_inference_whitespace_text_returns_silence() -> None:
    # A whitespace-only chunk has no speakable pieces; _run_inference must return
    # brief silence rather than feeding "" to XTTS (which crashes inference).
    engine = XTTSEngine(Config.from_env())
    engine._model = object()  # sentinel: the empty path returns before using it
    engine.sample_rate = 24000
    out = engine._run_inference("   ")
    assert isinstance(out, np.ndarray)
    assert out.dtype == np.float32
    assert len(out) > 0
    assert not out.any()  # all zeros


def test_run_inference_rejects_concurrent_call() -> None:
    # While the single-flight lock is held (simulating an orphaned post-timeout
    # inference thread still on the GPU), a new _run_inference must reject with
    # InferenceBusyError instead of starting a second concurrent GPU inference.
    engine = XTTSEngine(Config.from_env())
    engine._model = object()
    engine.sample_rate = 24000
    assert engine._gpu_lock.acquire(blocking=False)
    try:
        with pytest.raises(InferenceBusyError):
            engine._run_inference("hello there")
    finally:
        engine._gpu_lock.release()


def test_run_inference_releases_lock_after_success() -> None:
    engine = XTTSEngine(Config.from_env())
    engine._model = object()  # whitespace path returns before using the model
    engine.sample_rate = 24000
    engine._run_inference("   ")
    # The lock must be free for the next call.
    assert engine._gpu_lock.acquire(blocking=False)
    engine._gpu_lock.release()


def test_run_inference_releases_lock_after_exception() -> None:
    engine = XTTSEngine(Config.from_env())
    engine.sample_rate = 24000

    class _BoomInner:
        def inference(self, **kwargs):
            raise RuntimeError("inference blew up")

    class _BoomModel:
        class synthesizer:  # noqa: N801
            tts_model = _BoomInner()

    engine._model = _BoomModel()
    engine._gpt_cond_latent = "latent"
    engine._speaker_embedding = "embed"
    with pytest.raises(RuntimeError):
        engine._run_inference("hello there")
    # A raised inference must still release the lock so the wrapper recovers.
    assert engine._gpu_lock.acquire(blocking=False)
    engine._gpu_lock.release()


def test_split_short_text_one_piece_per_sentence() -> None:
    pieces = _split_for_xtts("A short sentence. Another short one.")
    assert pieces == ["A short sentence.", "Another short one."]


def test_split_empty_and_whitespace() -> None:
    assert _split_for_xtts("") == []
    assert _split_for_xtts("   ") == []


def test_split_keeps_every_piece_under_cap() -> None:
    # ~1000 chars, no sentence breaks -> must be cut into <=cap pieces.
    long = ("word " * 200).strip() + "."
    pieces = _split_for_xtts(long)
    assert pieces
    assert all(len(p) <= _XTTS_MAX_CHARS for p in pieces)
    # No content dropped: every "word" survives across the pieces.
    assert "".join(pieces).count("word") == long.count("word")


def test_split_oversize_sentence_cut_at_word_boundary() -> None:
    sentence = ("alpha " * 60).strip() + "."  # ~360 chars, single sentence
    pieces = _split_for_xtts(sentence)
    assert len(pieces) >= 2
    assert all(len(p) <= _XTTS_MAX_CHARS for p in pieces)
    # Cuts happen at spaces, so no piece ends mid-"alpha".
    assert all(not p.endswith("alph") for p in pieces)
