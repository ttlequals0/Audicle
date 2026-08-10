"""Post-TTS chunk quality detector (audio_analysis).

Pure numpy/torch metrics -- no ffmpeg -- so these run unconditionally. Each
helper synthesizes a waveform with a known failure signature (flat drone, steady
noise, mostly-silent, over-long) or a speech-like signal that must pass.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from app.config import get_settings
from app.services import audio_analysis

_SR = 24000


def _drone(duration_secs: float = 1.0, freq: float = 440.0, amp: float = 0.5) -> torch.Tensor:
    n = round(duration_secs * _SR)
    t = torch.arange(n, dtype=torch.float32) / _SR
    return (amp * torch.sin(2 * torch.pi * freq * t)).unsqueeze(0)


def _white_noise(duration_secs: float = 1.0, amp: float = 0.1, seed: int = 0) -> torch.Tensor:
    n = round(duration_secs * _SR)
    rng = np.random.default_rng(seed)
    return torch.from_numpy((amp * rng.standard_normal(n)).astype(np.float32)).unsqueeze(0)


def _speechlike(duration_secs: float = 2.0, carrier: float = 180.0, amp: float = 0.6) -> torch.Tensor:
    """A carrier under a 4 Hz syllabic envelope with two silent pauses -- the
    high envelope variance a real narrated chunk has."""

    n = round(duration_secs * _SR)
    t = torch.arange(n, dtype=torch.float32) / _SR
    env = 0.5 + 0.5 * torch.sin(2 * torch.pi * 4.0 * t)
    env[(t > 0.7) & (t < 0.95)] = 0.0  # a pause between "words"
    env[(t > 1.5) & (t < 1.7)] = 0.0
    return (amp * torch.sin(2 * torch.pi * carrier * t) * env).unsqueeze(0)


def _mostly_silent(duration_secs: float = 2.0) -> torch.Tensor:
    n = round(duration_secs * _SR)
    wave = torch.zeros(n)
    island = round(0.05 * _SR)  # a 50 ms voiced blip in an otherwise empty chunk
    t = torch.arange(island, dtype=torch.float32) / _SR
    wave[:island] = 0.5 * torch.sin(2 * torch.pi * 200 * t)
    return wave.unsqueeze(0)


def test_flags_constant_tone_as_drone(env: Path) -> None:
    verdict = audio_analysis.analyze_chunk(_drone(), _SR, word_count=5, settings=get_settings())
    assert verdict.ok is False
    assert "flat_envelope" in verdict.reasons


def test_flags_white_noise(env: Path) -> None:
    verdict = audio_analysis.analyze_chunk(
        _white_noise(), _SR, word_count=5, settings=get_settings()
    )
    assert verdict.ok is False
    assert "broadband_noise" in verdict.reasons


def test_passes_speechlike(env: Path) -> None:
    verdict = audio_analysis.analyze_chunk(
        _speechlike(), _SR, word_count=5, settings=get_settings()
    )
    assert verdict.ok is True, verdict.reasons


def test_flags_overlong_duration(env: Path) -> None:
    # Envelope is fine, but 1 word can't fill 3 seconds -> repetition signature.
    verdict = audio_analysis.analyze_chunk(
        _speechlike(duration_secs=3.0), _SR, word_count=1, settings=get_settings()
    )
    assert verdict.ok is False
    assert "overlong" in verdict.reasons


def test_short_chunk_not_flagged_overlong(env: Path) -> None:
    # Regression (prod chunk 119): a 1-word chunk that reads in ~1s is normal,
    # not "overlong". The plain words-per-second model called it ~3.5x too long
    # because it ignores the fixed per-chunk overhead (silence + a single word).
    verdict = audio_analysis.analyze_chunk(
        _speechlike(duration_secs=1.0), _SR, word_count=1, settings=get_settings()
    )
    assert "overlong" not in verdict.reasons


def test_flags_mostly_silent(env: Path) -> None:
    verdict = audio_analysis.analyze_chunk(
        _mostly_silent(), _SR, word_count=5, settings=get_settings()
    )
    assert verdict.ok is False
    assert "mostly_silent" in verdict.reasons


def test_short_chunk_is_ok(env: Path) -> None:
    # Shorter than one analysis frame -> degenerate verdict, never flagged.
    verdict = audio_analysis.analyze_chunk(
        torch.zeros((1, 100)), _SR, word_count=1, settings=get_settings()
    )
    assert verdict.ok is True


def test_rms_cv_orders_speech_above_drone(env: Path) -> None:
    settings = get_settings()
    speech = audio_analysis.analyze_chunk(_speechlike(), _SR, 5, settings)
    drone = audio_analysis.analyze_chunk(_drone(), _SR, 5, settings)
    # Ordering is robust even if absolute thresholds drift during tuning.
    assert speech.metrics.rms_cv > drone.metrics.rms_cv


def test_analyze_wav_path_loads_and_flags(env: Path, tmp_path: Path) -> None:
    path = tmp_path / "chunk.wav"
    sf.write(str(path), _drone().squeeze(0).numpy(), _SR, subtype="PCM_16")
    verdict = audio_analysis.analyze_wav_path(path, word_count=5, settings=get_settings())
    assert verdict.ok is False


def test_windowed_gate_flags_localized_drone(env: Path) -> None:
    # A 5 s tone inside 25 s of speech is ~20% of the chunk: too diluted for the
    # whole-chunk reductions, caught by the windowed pass (2a).
    speech = _speechlike(duration_secs=25.0)
    drone = _drone(duration_secs=5.0, freq=217.0, amp=0.35)
    spliced = torch.cat(
        [speech[:, : 10 * _SR], drone, speech[:, 10 * _SR : 20 * _SR]], dim=1
    )
    settings = get_settings()
    clean = audio_analysis.analyze_chunk(speech, _SR, word_count=65, settings=settings)
    assert clean.ok is True, clean.reasons
    verdict = audio_analysis.analyze_chunk(spliced, _SR, word_count=65, settings=settings)
    assert verdict.ok is False
    assert "flat_envelope" in verdict.reasons


def test_worst_window_metrics_reported(env: Path) -> None:
    speech = _speechlike(duration_secs=25.0)
    drone = _drone(duration_secs=5.0, freq=217.0, amp=0.35)
    spliced = torch.cat(
        [speech[:, : 10 * _SR], drone, speech[:, 10 * _SR : 20 * _SR]], dim=1
    )
    settings = get_settings()
    metrics = audio_analysis.analyze_chunk(
        spliced, _SR, word_count=65, settings=settings
    ).metrics
    assert metrics.worst_window_rms_cv < metrics.rms_cv
    assert metrics.worst_window_rms_cv < settings.AUDIO_ANALYSIS_MIN_RMS_CV
    assert metrics.worst_window_crest < settings.AUDIO_ANALYSIS_MIN_CREST


def test_median_f0_matches_carrier(env: Path) -> None:
    metrics = audio_analysis.analyze_chunk(
        _speechlike(duration_secs=5.0, carrier=180.0), _SR, word_count=12, settings=get_settings()
    ).metrics
    assert 170.0 <= metrics.median_f0_hz <= 190.0


def test_median_f0_zero_for_noise(env: Path) -> None:
    metrics = audio_analysis.analyze_chunk(
        _white_noise(duration_secs=2.0), _SR, word_count=5, settings=get_settings()
    ).metrics
    assert metrics.median_f0_hz == 0.0


def test_pitch_tracker_warms_up_then_flags_semitone_drift(env: Path) -> None:
    settings = get_settings()
    tracker = audio_analysis.PitchTracker(settings)
    # No reference until AUDIO_ANALYSIS_F0_WARMUP_CHUNKS chunks are accepted.
    assert tracker.deviation_semitones(200.0) is None
    for f0 in (100.0, 101.0, 99.0):
        tracker.accept(f0)
    # 119 Hz against a 100 Hz reference is ~3 semitones.
    dev = tracker.deviation_semitones(119.0)
    assert dev is not None and 2.5 < dev < 3.5
    # An unmeasured chunk (f0=0) is never judged.
    assert tracker.deviation_semitones(0.0) is None
