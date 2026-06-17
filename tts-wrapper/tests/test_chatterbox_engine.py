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
    # The chatterbox knobs and sample_rate come from from_env defaults
    # (exaggeration 0.0, cfg_weight 0.0, temperature 0.5, sample_rate 24000).
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
    # _run_inference seeds before generating (default seed != 0); a no-op torch
    # stub lets the inference tests run without the real torch dependency.
    engine._torch = types.SimpleNamespace(
        manual_seed=lambda _s: None,
        cuda=types.SimpleNamespace(is_available=lambda: False, manual_seed_all=lambda _s: None),
    )
    return engine


# --- attributes / factory --------------------------------------------------


def test_engine_attributes_and_lazy_construction() -> None:
    # Constructing must not import torch/chatterbox (only load() does).
    engine = ChatterboxEngine(_config())
    assert engine.name == "chatterbox"
    assert engine.model_loaded is False
    assert engine.reference_loaded is False


def test_factory_returns_chatterbox_engine() -> None:
    from main import _default_engine_factory

    assert isinstance(_default_engine_factory(), ChatterboxEngine)


def test_from_env_chatterbox_seed_and_temperature_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHATTERBOX_SEED", raising=False)
    monkeypatch.delenv("CHATTERBOX_TEMPERATURE", raising=False)
    cfg = Config.from_env()
    # Determinism on by default; temperature below Turbo's 0.8 to cut sampling
    # variance (the "right dozens of times then wrong once" failure).
    assert cfg.chatterbox_seed == 1234
    assert cfg.chatterbox_temperature == 0.5


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


def test_boot_reference_path_picks_lowest_filled_slot(tmp_path: Path) -> None:
    engine = _loaded_engine()
    object.__setattr__(engine.config, "reference_path", str(tmp_path / "voice.wav"))
    voices = tmp_path / "voices"
    voices.mkdir()
    assert engine._boot_reference_path() is None  # no slots filled
    (voices / "slot3.wav").write_bytes(b"x")
    (voices / "slot2.wav").write_bytes(b"x")
    assert engine._boot_reference_path() == voices / "slot2.wav"  # lowest filled wins


async def test_reload_reference_no_slots_is_noop(tmp_path: Path) -> None:
    engine = _loaded_engine()
    engine.config = Config.from_env()
    object.__setattr__(engine.config, "reference_path", str(tmp_path / "voice.wav"))
    # No voices/ dir -> no slots -> reload is a graceful no-op (must not raise).
    await engine.reload_reference()


async def test_reload_reference_rolls_back_on_failure(tmp_path: Path) -> None:
    slot = tmp_path / "voices" / "slot1.wav"
    slot.parent.mkdir(parents=True)
    slot.write_bytes(b"not really a wav")
    model = FakeChatterboxModel(prepare_raises=True)
    engine = _loaded_engine(model)
    object.__setattr__(engine.config, "reference_path", str(tmp_path / "voice.wav"))
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


def test_run_inference_seeds_before_generate_when_seed_nonzero() -> None:
    model = FakeChatterboxModel()
    engine = _loaded_engine(model)
    object.__setattr__(engine.config, "chatterbox_seed", 1234)
    seeded: list[int] = []
    engine._torch = types.SimpleNamespace(
        manual_seed=lambda s: seeded.append(s),
        cuda=types.SimpleNamespace(is_available=lambda: False, manual_seed_all=lambda s: None),
    )
    engine._run_inference("Hello world.")
    assert seeded == [1234]
    assert model.generate_calls == ["Hello world."]


def test_run_inference_skips_seed_when_zero() -> None:
    model = FakeChatterboxModel()
    engine = _loaded_engine(model)
    object.__setattr__(engine.config, "chatterbox_seed", 0)
    seeded: list[int] = []
    engine._torch = types.SimpleNamespace(
        manual_seed=lambda s: seeded.append(s),
        cuda=types.SimpleNamespace(is_available=lambda: False, manual_seed_all=lambda s: None),
    )
    engine._run_inference("Hello world.")
    assert seeded == []


def test_run_inference_seed_override_beats_config_seed() -> None:
    # A per-request seed (sent on a quality regeneration) must win over the
    # configured seed so the re-gen produces different audio.
    model = FakeChatterboxModel()
    engine = _loaded_engine(model)
    object.__setattr__(engine.config, "chatterbox_seed", 1234)
    seeded: list[int] = []
    engine._torch = types.SimpleNamespace(
        manual_seed=lambda s: seeded.append(s),
        cuda=types.SimpleNamespace(is_available=lambda: False, manual_seed_all=lambda s: None),
    )
    engine._run_inference("Hello world.", seed=999)
    assert seeded == [999]


def test_set_seed_masks_out_of_range_for_numpy() -> None:
    # np.random.seed rejects values outside [0, 2**32-1]; a large/negative
    # operator seed must be masked rather than crash inference.
    model = FakeChatterboxModel()
    engine = _loaded_engine(model)
    object.__setattr__(engine.config, "chatterbox_seed", 2**40 + 7)
    engine._torch = types.SimpleNamespace(
        manual_seed=lambda _s: None,
        cuda=types.SimpleNamespace(is_available=lambda: False, manual_seed_all=lambda _s: None),
    )
    engine._run_inference("Hello world.")  # must not raise ValueError from numpy


async def test_synthesize_encodes_mono_pcm16_wav() -> None:
    model = FakeChatterboxModel(piece_secs=0.2)
    engine = _loaded_engine(model)
    # synthesize touches torch for the CUDA-OOM except clause and for seeding; a
    # stub with a never-raised OutOfMemoryError plus no-op seed calls lets us
    # exercise the encode path without torch.
    engine._torch = types.SimpleNamespace(
        manual_seed=lambda _s: None,
        cuda=types.SimpleNamespace(
            OutOfMemoryError=RuntimeError,
            empty_cache=lambda: None,
            is_available=lambda: False,
            manual_seed_all=lambda _s: None,
        ),
    )
    wav_bytes = await engine.synthesize("Hello world.")
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 24000
        assert wf.getnframes() == int(24000 * 0.2)
