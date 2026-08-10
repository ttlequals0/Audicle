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

# F0 estimation bounds: the plausible narration range. Frames outside it, or
# with a weak autocorrelation peak (unvoiced/noise), do not vote.
_F0_MIN_HZ = 60.0
_F0_MAX_HZ = 400.0
_F0_FRAME_SECS = 0.05  # 50 ms: >= 3 periods at the 60 Hz floor
_F0_HOP_SECS = 0.10
_F0_MIN_CONFIDENCE = 0.3


@dataclass(frozen=True)
class ChunkMetrics:
    """The signals that decide a verdict (each maps to a reason), kept so
    the pipeline can log them for threshold tuning.

    The worst_window_* triple comes from the flattest sliding window (lowest
    envelope CV): a drone localized inside a long chunk is diluted out of the
    whole-chunk reductions but dominates its own window."""

    rms_cv: float
    crest_factor: float
    zero_crossing_rate: float
    silent_fraction: float
    duration_ratio: float
    worst_window_rms_cv: float = 0.0
    worst_window_crest: float = 0.0
    worst_window_zcr: float = 0.0
    median_f0_hz: float = 0.0  # 0.0 = no voiced frames with a confident pitch


@dataclass(frozen=True)
class ChunkVerdict:
    ok: bool
    reasons: tuple[str, ...]
    metrics: ChunkMetrics


def _frame_signal(mono: np.ndarray, frame_n: int, hop_n: int) -> np.ndarray:
    """Non-copying (n_frames, frame_n) view via sliding windows, strided by hop."""

    windows = np.lib.stride_tricks.sliding_window_view(mono, frame_n)
    return windows[::hop_n]


def _envelope_reasons(
    rms_cv: float, crest: float, zcr: float, settings: Settings
) -> tuple[str, ...]:
    """Drone/noise reasons for one (rms_cv, crest, zcr) triple, at any scope.

    A flat drone is low-variance AND non-peaky (two independent signals, so a
    legitimate steady-but-peaky passage isn't falsely flagged); steady
    broadband noise is low-variance but high zero-crossing."""

    if rms_cv >= settings.AUDIO_ANALYSIS_MIN_RMS_CV:
        return ()
    reasons = []
    if crest < settings.AUDIO_ANALYSIS_MIN_CREST:
        reasons.append("flat_envelope")
    if zcr > settings.AUDIO_ANALYSIS_MAX_ZCR:
        reasons.append("broadband_noise")
    return tuple(reasons)


def _window_metrics(
    mono: np.ndarray,
    frame_rms: np.ndarray,
    voiced: np.ndarray,
    frame_zc: np.ndarray,
    frame_n: int,
    hop_n: int,
    settings: Settings,
) -> list[tuple[float, float, float]]:
    """(rms_cv, crest, zcr) per sliding window of AUDIO_ANALYSIS_WINDOW_SECS at
    50 percent overlap. Empty when the chunk fits in one window (the whole-chunk
    reductions already cover it). Windows with fewer than two voiced frames are
    skipped: nothing to judge."""

    n_frames = frame_rms.shape[0]
    win_frames = max(
        2, round(settings.AUDIO_ANALYSIS_WINDOW_SECS * 1000 / settings.AUDIO_ANALYSIS_HOP_MS)
    )
    if n_frames <= win_frames:
        return []
    step = max(1, win_frames // 2)
    starts = list(range(0, n_frames - win_frames + 1, step))
    if starts[-1] != n_frames - win_frames:
        starts.append(n_frames - win_frames)

    out: list[tuple[float, float, float]] = []
    for start in starts:
        end = start + win_frames
        win_voiced = voiced[start:end]
        if win_voiced.sum() < 2:
            continue
        win_rms = frame_rms[start:end][win_voiced]
        cv = float(win_rms.std() / (win_rms.mean() + _EPS))
        seg = mono[start * hop_n : min((end - 1) * hop_n + frame_n, mono.shape[0])]
        crest = float(np.max(np.abs(seg))) / (float(np.sqrt(np.mean(seg**2))) + _EPS)
        zcr = float(frame_zc[start:end][win_voiced].mean())
        out.append((cv, crest, zcr))
    return out


def _median_f0(mono: np.ndarray, sample_rate: int, floor: float) -> float:
    """Median F0 over voiced frames via FFT autocorrelation.

    Zero-padding to 2x the frame makes the autocorrelation linear, so it decays
    with lag and the fundamental's peak beats its subharmonics; frames whose
    normalized peak is under _F0_MIN_CONFIDENCE (unvoiced, noise) do not vote.
    Returns 0.0 when nothing votes."""

    frame_n = round(_F0_FRAME_SECS * sample_rate)
    hop_n = max(1, round(_F0_HOP_SECS * sample_rate))
    if sample_rate <= 0 or mono.shape[0] < frame_n or frame_n < 2:
        return 0.0
    frames = _frame_signal(mono, frame_n, hop_n)
    rms = np.sqrt(np.mean(frames**2, axis=1))
    frames = frames[rms > floor]
    if frames.shape[0] == 0:
        return 0.0
    lag_min = max(1, int(sample_rate / _F0_MAX_HZ))
    lag_max = min(frame_n - 1, int(sample_rate / _F0_MIN_HZ))
    if lag_max <= lag_min:
        return 0.0
    centered = frames - frames.mean(axis=1, keepdims=True)
    nfft = 1 << int(np.ceil(np.log2(2 * frame_n)))
    spec = np.fft.rfft(centered, n=nfft, axis=1)
    ac = np.fft.irfft(spec * np.conj(spec), n=nfft, axis=1)
    norm = ac[:, lag_min : lag_max + 1] / (ac[:, :1] + _EPS)
    best = np.argmax(norm, axis=1)
    conf = norm[np.arange(norm.shape[0]), best]
    f0 = sample_rate / (best + lag_min)
    f0 = f0[conf > _F0_MIN_CONFIDENCE]
    return float(np.median(f0)) if f0.size else 0.0


class PitchTracker:
    """Running pitch reference over a job's accepted chunks (the 2b gate).

    The reference is the median F0 of chunks that passed the quality gate; no
    verdict is issued until AUDIO_ANALYSIS_F0_WARMUP_CHUNKS have been accepted,
    otherwise the first bad chunk would anchor the episode's reference."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._accepted: list[float] = []

    @property
    def reference_hz(self) -> float | None:
        if len(self._accepted) < self._settings.AUDIO_ANALYSIS_F0_WARMUP_CHUNKS:
            return None
        return float(np.median(self._accepted))

    def deviation_semitones(self, f0_hz: float) -> float | None:
        """Absolute deviation from the reference, or None when either side is
        unmeasured (warmup not done, or the chunk had no confident pitch)."""

        reference = self.reference_hz
        if reference is None or f0_hz <= 0:
            return None
        return abs(12.0 * float(np.log2(f0_hz / reference)))

    def accept(self, f0_hz: float) -> None:
        if f0_hz > 0:
            self._accepted.append(f0_hz)


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

    # Per-frame zero-crossing rate (noise corroboration). Needs frame_n >= 2
    # for a crossing to exist and to avoid a 0/(frame_n-1) NaN. Computed for
    # all frames so the windowed pass can slice it.
    if frame_n >= 2:
        signs = np.sign(frames)
        frame_zc = (np.abs(np.diff(signs, axis=1)) > 0).sum(axis=1) / (frame_n - 1)
    else:
        frame_zc = np.zeros(frames.shape[0], dtype=np.float32)
    zero_crossing_rate = (
        float(frame_zc[voiced].mean()) if voiced.any() and frame_n >= 2 else 0.0
    )

    # Windowed pass: a drone occupying a fraction of a long chunk is diluted
    # out of the whole-chunk reductions but dominates its own 3 s window.
    windows = _window_metrics(mono, frame_rms, voiced, frame_zc, frame_n, hop_n, settings)
    tripped = [w for w in windows if _envelope_reasons(*w, settings)]
    if tripped:
        # Log the tripping window (lowest CV among them), so the worst_window_*
        # metrics describe the window that actually failed the gate.
        worst_cv, worst_crest, worst_zcr = min(tripped, key=lambda w: w[0])
    elif windows:
        worst_cv, worst_crest, worst_zcr = min(windows, key=lambda w: w[0])
    else:
        worst_cv, worst_crest, worst_zcr = rms_cv, crest_factor, zero_crossing_rate

    metrics = ChunkMetrics(
        rms_cv=rms_cv,
        crest_factor=crest_factor,
        zero_crossing_rate=zero_crossing_rate,
        silent_fraction=silent_fraction,
        duration_ratio=duration_ratio,
        worst_window_rms_cv=worst_cv,
        worst_window_crest=worst_crest,
        worst_window_zcr=worst_zcr,
        median_f0_hz=_median_f0(mono, sample_rate, floor),
    )

    # One detector, two scopes: the whole-chunk triple catches chunk-wide
    # flatness, each window catches a localized stretch. The verdict is the
    # union of every scope's reasons.
    reasons: list[str] = []
    scoped = [
        (rms_cv, crest_factor, zero_crossing_rate),
        *tripped,
    ]
    for triple in scoped:
        for reason in _envelope_reasons(*triple, settings):
            if reason not in reasons:
                reasons.append(reason)
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
