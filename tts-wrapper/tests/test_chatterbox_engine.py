"""ChatterboxEngine tests that don't require torch / chatterbox-tts / a GPU.

The heavy deps are imported lazily inside ``load()``, so the module imports and
the split/join/encode + reference-lifecycle logic are testable here with a fake
model. Real clone fidelity and inference are validated on the GPU host.
"""

from __future__ import annotations

import io
import types
import wave
from pathlib import Path

import numpy as np
import pytest

from chatterbox_engine import ChatterboxEngine
from config import Config
from engine import InferenceBusyError


def _config() -> Config:
    # Engine field is irrelevant to ChatterboxEngine construction; the chatterbox
    # knobs and sample_rate come from from_env defaults (0.0/0.0/0.8, 24000).
    return Config.from_env()


class _FakeTensor:
    """Mimics the (1, N) torch.Tensor returned by ChatterboxTTS.generate."""

    def __init__(self, arr: np.ndarray) -> None:
        self._arr = arr

    def squeeze(self, _dim):
        return self

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self._arr


class FakeChatterboxModel:
    """Stand-in for ChatterboxTurboTTS; records calls, returns fixed-length audio."""

    sr = 24000

    def __init__(self, *, piece_secs: float = 0.1, prepare_raises: bool = False) -> None:
        self.conds = None
        self.prepare_calls: list[tuple[str, float]] = []
        self.generate_calls: list[str] = []
        self._piece_len = int(self.sr * piece_secs)
        self._prepare_raises = prepare_raises

    def prepare_conditionals(self, wav_fpath, exaggeration=0.5):
        if self._prepare_raises:
            raise RuntimeError("simulated decode failure")
        self.prepare_calls.append((wav_fpath, exaggeration))
        self.conds = object()

    def generate(self, text, exaggeration=0.0, cfg_weight=0.0, temperature=0.8):
        # No audio_prompt_path parameter: if the engine ever passed one this would
        # TypeError, which is the contract we want (reuse cached conditionals).
        self.generate_calls.append(text)
        return _FakeTensor(np.full(self._piece_len, 0.5, dtype=np.float32))


def _loaded_engine(model: FakeChatterboxModel | None = None) -> ChatterboxEngine:
    engine = ChatterboxEngine(_config())
    engine._model = model or FakeChatterboxModel()
    engine.sample_rate = engine._model.sr
    return engine


# --- attributes / factory --------------------------------------------------


def test_engine_attributes_and_lazy_construction() -> None:
    # Constructing must not import torch/chatterbox (only load() does).
    engine = ChatterboxEngine(_config())
    assert engine.name == "chatterbox"
    assert engine.supports_phonemes is False
    assert engine.model_loaded is False
    assert engine.reference_loaded is False


def test_factory_selects_chatterbox_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTS_ENGINE", "chatterbox")
    from main import _default_engine_factory

    assert isinstance(_default_engine_factory(), ChatterboxEngine)


# --- reference lifecycle ---------------------------------------------------


def test_prepare_reference_sets_loaded_and_bakes_exaggeration() -> None:
    model = FakeChatterboxModel()
    engine = _loaded_engine(model)
    engine._prepare_reference(Path("/ref/voice.wav"))
    assert engine.reference_loaded is True
    # exaggeration is baked at prepare time (default 0.0 == neutral read).
    assert model.prepare_calls == [("/ref/voice.wav", 0.0)]


def test_prepare_reference_rejects_when_gpu_busy() -> None:
    engine = _loaded_engine()
    engine._gpu_lock.acquire()
    try:
        with pytest.raises(InferenceBusyError):
            engine._prepare_reference(Path("/ref/voice.wav"))
    finally:
        engine._gpu_lock.release()


async def test_reload_reference_missing_file_raises(tmp_path: Path) -> None:
    engine = _loaded_engine()
    engine.config = Config.from_env()
    object.__setattr__(engine.config, "reference_path", str(tmp_path / "absent.wav"))
    with pytest.raises(FileNotFoundError):
        await engine.reload_reference()


async def test_reload_reference_rolls_back_on_failure(tmp_path: Path) -> None:
    ref = tmp_path / "voice.wav"
    ref.write_bytes(b"not really a wav")
    model = FakeChatterboxModel(prepare_raises=True)
    engine = _loaded_engine(model)
    object.__setattr__(engine.config, "reference_path", str(ref))
    engine.reference_loaded = True  # a good voice was previously loaded
    with pytest.raises(RuntimeError):
        await engine.reload_reference()
    # Failed recompute must restore the prior good state, not leave it False.
    assert engine.reference_loaded is True


# --- inference -------------------------------------------------------------


def test_run_inference_single_piece_calls_generate_once() -> None:
    model = FakeChatterboxModel()
    engine = _loaded_engine(model)
    out = engine._run_inference("Hello world.")
    assert isinstance(out, np.ndarray)
    assert model.generate_calls == ["Hello world."]
    assert len(out) == int(model.sr * 0.1)


def test_run_inference_joins_pieces_with_silence_gap() -> None:
    model = FakeChatterboxModel(piece_secs=0.1)
    engine = _loaded_engine(model)
    out = engine._run_inference("One. Two.")
    assert model.generate_calls == ["One.", "Two."]
    piece = int(model.sr * 0.1)
    gap = int(model.sr * 0.12)
    assert len(out) == piece + gap + piece


def test_run_inference_empty_text_returns_short_silence() -> None:
    model = FakeChatterboxModel()
    engine = _loaded_engine(model)
    out = engine._run_inference("   ")
    assert model.generate_calls == []
    assert len(out) == int(model.sr * 0.05)


def test_run_inference_rejects_when_gpu_busy() -> None:
    engine = _loaded_engine()
    engine._gpu_lock.acquire()
    try:
        with pytest.raises(InferenceBusyError):
            engine._run_inference("Hello.")
    finally:
        engine._gpu_lock.release()


async def test_synthesize_encodes_mono_pcm16_wav() -> None:
    model = FakeChatterboxModel(piece_secs=0.2)
    engine = _loaded_engine(model)
    # synthesize only touches torch for the CUDA-OOM except clause; a stub with a
    # never-raised OutOfMemoryError lets us exercise the encode path without torch.
    engine._torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(OutOfMemoryError=RuntimeError, empty_cache=lambda: None)
    )
    wav_bytes = await engine.synthesize("Hello world.")
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 24000
        assert wf.getnframes() == int(24000 * 0.2)
