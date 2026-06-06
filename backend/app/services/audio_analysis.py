"""Post-TTS per-chunk quality analysis.

Chatterbox occasionally returns a chunk that is not speech: a flat band of
noise, a steady drone/buzz, near-silence, or an over-long repetition. This
module scores a chunk waveform with cheap numpy metrics so the pipeline can
regenerate a bad chunk (the wrapper is non-deterministic, so a re-gen usually
recovers).

Metrics (all single-pass numpy reductions on the mono signal, ~ms per chunk):

- rms_cv: coefficient of variation of the per-frame RMS envelope over voiced
  frames. Speech has a strongly modulated envelope (high CV); a flat drone or
  steady noise has a near-constant envelope (low CV).
- crest_factor: peak / RMS. A steady tone is non-peaky (~1.4); speech is peaky.
- zero_crossing_rate: high and steady for broadband noise.
- silent_fraction: fraction of frames below the silence floor.
- duration_ratio: actual duration vs the duration the word count implies;
  catches repetition (too long) and truncation (too short).

Only torch/numpy are used so this runs on the CPU-only backend with no new deps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import torch

from app.config import Settings
from app.services.audio import AudioError, _load_wav

logger = logging.getLogger("app.services.audio_analysis")

_EPS = 1e-9


@dataclass(frozen=True)
class ChunkMetrics:
    """The five signals that decide a verdict (each maps to a reason), kept so
    the pipeline can log them for threshold tuning."""

    rms_cv: float
    crest_factor: float
    zero_crossing_rate: float
    silent_fraction: float
    duration_ratio: float


@dataclass(frozen=True)
class ChunkVerdict:
    ok: bool
    reasons: tuple[str, ...]
    metrics: ChunkMetrics


def _frame_signal(mono: np.ndarray, frame_n: int, hop_n: int) -> np.ndarray:
    """Non-copying (n_frames, frame_n) view via sliding windows, strided by hop."""

    windows = np.lib.stride_tricks.sliding_window_view(mono, frame_n)
    return windows[::hop_n]


def analyze_chunk(
    waveform: torch.Tensor,
    sample_rate: int,
    word_count: int,
    settings: Settings,
) -> ChunkVerdict:
    """Score a (channels, samples) chunk waveform. Never raises on odd input:
    a chunk shorter than one frame returns a degenerate ok verdict."""

    mono = waveform.mean(dim=0).detach().cpu().numpy().astype(np.float32)
    n = mono.shape[0]
    duration_secs = n / sample_rate if sample_rate else 0.0

    frame_n = max(1, round(settings.AUDIO_ANALYSIS_FRAME_MS * sample_rate / 1000))
    hop_n = max(1, round(settings.AUDIO_ANALYSIS_HOP_MS * sample_rate / 1000))

    # Add a fixed per-chunk overhead so the words-per-second model doesn't call a
    # 1-2 word chunk (~1s of audio) "overlong" -- its silence + single-word floor
    # dominates the linear estimate.
    expected_secs = (
        settings.AUDIO_ANALYSIS_DURATION_OVERHEAD_SECS
        + word_count / settings.AUDIO_ANALYSIS_WORDS_PER_SEC
    ) if word_count else 0.0
    duration_ratio = duration_secs / expected_secs if expected_secs else 1.0

    # Too short to frame: don't flag (a tiny legitimate chunk must pass).
    if n < frame_n:
        metrics = ChunkMetrics(0.0, 0.0, 0.0, 0.0, duration_ratio)
        return ChunkVerdict(ok=True, reasons=(), metrics=metrics)

    frames = _frame_signal(mono, frame_n, hop_n)
    frame_rms = np.sqrt(np.mean(frames**2, axis=1))
    floor = settings.AUDIO_SILENCE_THRESHOLD
    voiced = frame_rms > floor
    silent_fraction = float(1.0 - voiced.mean())

    # Envelope CV over voiced frames only, so leading/trailing silence can't
    # inflate a drone's CV past the threshold and hide it.
    voiced_rms = frame_rms[voiced]
    rms_cv = float(voiced_rms.std() / (voiced_rms.mean() + _EPS)) if voiced_rms.size >= 2 else 0.0

    rms_overall = float(np.sqrt(np.mean(mono**2)))
    peak = float(np.max(np.abs(mono)))
    crest_factor = peak / (rms_overall + _EPS)

    # Mean zero-crossing rate over voiced frames (noise corroboration). Needs
    # frame_n >= 2 for a crossing to exist and to avoid a 0/(frame_n-1) NaN.
    if voiced.any() and frame_n >= 2:
        signs = np.sign(frames[voiced])
        zc = (np.abs(np.diff(signs, axis=1)) > 0).sum(axis=1) / (frame_n - 1)
        zero_crossing_rate = float(zc.mean())
    else:
        zero_crossing_rate = 0.0

    metrics = ChunkMetrics(
        rms_cv=rms_cv,
        crest_factor=crest_factor,
        zero_crossing_rate=zero_crossing_rate,
        silent_fraction=silent_fraction,
        duration_ratio=duration_ratio,
    )

    reasons: list[str] = []
    low_envelope = rms_cv < settings.AUDIO_ANALYSIS_MIN_RMS_CV
    # A flat drone is low-variance AND non-peaky (two independent signals, so a
    # legitimate steady-but-peaky passage isn't falsely flagged).
    if low_envelope and crest_factor < settings.AUDIO_ANALYSIS_MIN_CREST:
        reasons.append("flat_envelope")
    # Steady broadband noise is low-variance but high zero-crossing.
    if low_envelope and zero_crossing_rate > settings.AUDIO_ANALYSIS_MAX_ZCR:
        reasons.append("broadband_noise")
    if silent_fraction > settings.AUDIO_ANALYSIS_MAX_SILENT_FRACTION:
        reasons.append("mostly_silent")
    if expected_secs:
        if duration_ratio > settings.AUDIO_ANALYSIS_MAX_DURATION_RATIO:
            reasons.append("overlong")
        elif duration_ratio < settings.AUDIO_ANALYSIS_MIN_DURATION_RATIO:
            reasons.append("too_short")

    return ChunkVerdict(ok=not reasons, reasons=tuple(reasons), metrics=metrics)


def analyze_wav_path(wav_path, word_count: int, settings: Settings) -> ChunkVerdict:
    """Load a chunk WAV via the audio module and analyze it. Any load failure
    (missing/corrupt file -- soundfile raises its own error types) is converted
    to AudioError so the caller can uniformly treat it as 'pass'; analysis must
    never become a new failure mode."""

    # soundfile raises varied error types for a missing/corrupt file; convert
    # all of them to AudioError so the caller has one thing to catch.
    try:
        waveform, rate = _load_wav(wav_path)
    except Exception as exc:
        raise AudioError(f"could not load chunk WAV {wav_path}: {exc}") from exc
    if rate <= 0:
        raise AudioError(f"chunk WAV {wav_path} has non-positive sample rate {rate}")
    return analyze_chunk(waveform, rate, word_count, settings)
