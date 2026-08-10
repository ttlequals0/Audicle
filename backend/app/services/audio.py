"""Audio post-processing pipeline.

Inputs: per-chunk 24000 Hz mono WAV files written by the tts-wrapper to the
shared ``/data`` volume. Output: a single MP3 at ``/data/media/{id}.mp3``
plus a duration value read from the final file.

Stages (build-plan numbering):

1. Trim silence from each chunk WAV (torch-based detection).
2. Insert silence padding (torch zeros) between chunks.
3. Concat all chunks via tensor append, write the combined WAV with
   soundfile (a TorchCodec-free replacement for ``torchaudio.save``; the
   build-plan-mentioned ``torchaudio`` dep is dropped because torchaudio
   2.11 made TorchCodec the default save backend).
4. Normalize with the ebook2audiobook ffmpeg filter chain (loudnorm, EQ,
   denoise, gentle compression).
5. Encode to MP3 (libmp3lame, 24000 Hz, stereo upmix via -ac 2, 128k).
6. Read final MP3 duration via mutagen.

Per-chunk WAVs and the concatenated WAV are removed by the caller in a
``finally`` block; this module focuses on the data transformations.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from mutagen.id3 import APIC, ID3
from mutagen.mp3 import MP3

from app.config import Settings

logger = logging.getLogger("app.services.audio")


class AudioError(Exception):
    """Base class so callers can do a single except for any audio failure."""


class FfmpegError(AudioError):
    """ffmpeg exited non-zero. ``stderr`` captures the tail for the operator."""

    def __init__(self, returncode: int, stderr: str) -> None:
        super().__init__(f"ffmpeg failed with exit code {returncode}: {stderr[-400:]}")
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class EncodeResult:
    mp3_path: Path
    duration_secs: float


# Reference-voice transcode target: mono, 24 kHz, 16-bit PCM. The wrapper
# resamples internally, so this is just a canonical, broadly-compatible WAV.
_REFERENCE_WAV_SAMPLE_RATE = 24000


def transcode_to_wav(data: bytes, *, max_seconds: int = 70) -> bytes:
    """Decode an ffmpeg-readable audio upload (mp3, m4a, flac, ogg, opus, ...)
    and return mono 16-bit PCM WAV bytes. ``max_seconds`` caps the output so a
    long upload can't expand without bound. Raises :class:`FfmpegError`."""

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in"
        dst = Path(tmp) / "out.wav"
        src.write_bytes(data)
        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-t",
            str(max_seconds),
            "-ac",
            "1",
            "-ar",
            str(_REFERENCE_WAV_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(dst),
        ]
        # Bound wall-clock decode time: this path runs on untrusted uploads, and a
        # crafted file could otherwise hang ffmpeg and pin the calling thread.
        try:
            completed = subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=60
            )
        except subprocess.TimeoutExpired as exc:
            raise FfmpegError(-1, f"ffmpeg transcode timed out after {exc.timeout}s") from exc
        if completed.returncode != 0 or not dst.is_file():
            raise FfmpegError(completed.returncode, completed.stderr)
        return dst.read_bytes()


def wav_duration_secs(path: Path) -> float:
    """Duration of a WAV in seconds (via soundfile's header, no full decode)."""

    info = sf.info(str(path))
    return float(info.duration)


def embed_cover(mp3_path: Path, cover_jpg_bytes: bytes) -> None:
    """Embed ``cover_jpg_bytes`` into the MP3 as an ID3v2.3 front-cover (APIC) frame, so
    players that read only embedded art (Pocket Casts) show per-episode artwork. Writes
    ID3v2.3 with a latin-1 description -- the most broadly supported tag version, since v2.4
    APIC is read inconsistently by podcast clients. Replaces any existing APIC so a reprocess
    doesn't stack covers. Tags a temp copy and atomically replaces the original, so an
    interrupted tag write can't corrupt the episode."""

    tmp = mp3_path.with_name(f".cover-{mp3_path.name}")
    try:
        shutil.copyfile(mp3_path, tmp)
        tagged = MP3(tmp, ID3=ID3)
        if tagged.tags is None:
            tagged.add_tags()
        tagged.tags.delall("APIC")
        tagged.tags.add(
            APIC(encoding=0, mime="image/jpeg", type=3, desc="Cover", data=cover_jpg_bytes)
        )
        tagged.save(v2_version=3)
        os.replace(tmp, mp3_path)
    finally:
        # missing_ok only swallows FileNotFoundError; suppress the rest so a cleanup
        # error can't mask the real failure (the pipeline logs that one).
        with contextlib.suppress(OSError):
            tmp.unlink()


# --- Stage 1: silence trim --------------------------------------------------


def trim_silence(
    waveform: torch.Tensor,
    sample_rate: int,
    settings: Settings,
) -> torch.Tensor:
    """Trim leading and trailing silence from a mono ``waveform`` tensor.

    Algorithm (ebook2audiobook-derived): compute per-sample absolute amplitude,
    mark samples below ``AUDIO_SILENCE_THRESHOLD`` as silence, expand the
    kept region by ``AUDIO_SILENCE_BUFFER_MS`` on both ends. Returns the
    trimmed waveform; if every sample is silent, returns the original (so
    the chunk isn't accidentally erased).
    """

    if waveform.dim() != 2:
        raise AudioError(
            f"trim_silence expects a 2-D (channels, samples) tensor, got {waveform.shape}"
        )

    abs_wave = waveform.abs().mean(dim=0)
    above = abs_wave > settings.AUDIO_SILENCE_THRESHOLD
    if not above.any():
        return waveform

    indices = torch.nonzero(above, as_tuple=False).squeeze(1)
    start = int(indices[0].item())
    end = int(indices[-1].item()) + 1

    buffer_samples = round(settings.AUDIO_SILENCE_BUFFER_MS * sample_rate / 1000)
    start = max(0, start - buffer_samples)
    end = min(waveform.size(1), end + buffer_samples)
    return waveform[:, start:end]


def compress_internal_silence(
    waveform: torch.Tensor,
    sample_rate: int,
    settings: Settings,
) -> torch.Tensor:
    """Shorten internal silence runs that exceed ``AUDIO_MAX_INTERNAL_SILENCE_MS``.

    Chatterbox occasionally generates multi-second dead air inside a piece; the
    wrapper joins pieces into one chunk WAV, so that silence lands mid-chunk
    where :func:`trim_silence` (edges only) can't reach it. Any run of
    below-threshold samples longer than the cap is cut down to
    ``AUDIO_INTERNAL_SILENCE_KEEP_MS``, half kept at each end of the run so the
    speech decay/attack around it survives. A cap of 0 disables the pass; an
    all-silent waveform is returned unchanged (mirrors trim_silence: a chunk is
    never erased). Oversized runs touching the waveform's edges are compressed
    too (keep_half retained at the boundary); in the pipeline trim_silence has
    already removed those, so this only matters to other callers.
    """

    if waveform.dim() != 2:
        raise AudioError(
            f"compress_internal_silence expects a 2-D (channels, samples) tensor, "
            f"got {waveform.shape}"
        )
    max_ms = settings.AUDIO_MAX_INTERNAL_SILENCE_MS
    if max_ms <= 0:
        return waveform

    silent = waveform.abs().mean(dim=0) <= settings.AUDIO_SILENCE_THRESHOLD
    if silent.all():
        return waveform

    max_run = round(max_ms * sample_rate / 1000)
    # Half the kept duration goes to each end of a shortened run. Clamp both
    # misconfig directions: KEEP >= MAX can't duplicate samples, and a negative
    # KEEP can't eat speech on either side of the run.
    keep_half = max(
        0,
        min(
            round(settings.AUDIO_INTERNAL_SILENCE_KEEP_MS * sample_rate / 2000),
            max_run // 2,
        ),
    )

    edges = torch.diff(silent.to(torch.int8))
    starts = (torch.nonzero(edges == 1, as_tuple=False).squeeze(1) + 1).tolist()
    ends = (torch.nonzero(edges == -1, as_tuple=False).squeeze(1) + 1).tolist()
    if bool(silent[0]):
        starts.insert(0, 0)
    if bool(silent[-1]):
        ends.append(waveform.size(1))

    pieces: list[torch.Tensor] = []
    cursor = 0
    for start, end in zip(starts, ends, strict=True):
        if end - start <= max_run:
            continue
        pieces.append(waveform[:, cursor : start + keep_half])
        cursor = end - keep_half
    if not pieces:  # no run exceeded the cap
        return waveform
    pieces.append(waveform[:, cursor:])
    return torch.cat(pieces, dim=1)


# --- Stage 2 + 3: concat with silence padding -------------------------------


def concat_with_padding(
    chunk_paths: list[Path],
    output_path: Path,
    settings: Settings,
) -> tuple[Path, int, list[float]]:
    """Load each chunk WAV, trim edge silence, compress internal silence,
    append silence padding between chunks, write the concatenated WAV.

    Returns ``(output_path, sample_rate, chunk_durations)``: the sample rate so
    subsequent ffmpeg invocations can pin it explicitly, and each chunk's final
    post-trim duration in seconds so the transcript stage can build VTT cues
    that match the produced audio (the wrapper-reported durations are pre-trim
    and drift). Padding is inserted *between* chunks only -- there's no leading
    or trailing pad.
    """

    if not chunk_paths:
        raise AudioError("concat_with_padding called with zero chunks")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    pieces: list[torch.Tensor] = []
    durations: list[float] = []
    sample_rate: int | None = None
    channels: int | None = None
    pad_tensor: torch.Tensor | None = None

    for index, path in enumerate(chunk_paths):
        wave, rate = _load_wav(path)
        if sample_rate is None:
            sample_rate = rate
            channels = wave.size(0)
        elif rate != sample_rate:
            raise AudioError(
                f"chunk {index} has sample rate {rate} but earlier chunk had {sample_rate}"
            )
        elif wave.size(0) != channels:
            raise AudioError(
                f"chunk {index} has {wave.size(0)} channels but earlier chunk had {channels}"
            )
        wave = trim_silence(wave, rate, settings)
        wave = compress_internal_silence(wave, rate, settings)
        durations.append(wave.size(1) / rate)
        if index > 0:
            if pad_tensor is None:
                pad_n = round(settings.TTS_CHUNK_SILENCE_MS * sample_rate / 1000)
                # Match the channel count of the input WAVs so torch.cat doesn't
                # crash opaquely on stereo (or future multi-channel) chunks.
                pad_tensor = torch.zeros((channels, pad_n), dtype=wave.dtype)
            pieces.append(pad_tensor)
        pieces.append(wave)

    assert sample_rate is not None
    combined = torch.cat(pieces, dim=1)
    _save_wav(output_path, combined, sample_rate)
    return output_path, sample_rate, durations


def append_clip(
    combined_path: Path,
    clip_path: Path,
    *,
    lead_silence_ms: int = 700,
) -> None:
    """Append ``clip_path`` (an end-of-episode chime) to the combined episode WAV,
    in place, after a short lead silence. Both WAVs are written by our own pipeline at
    the same rate/channels (``transcode_to_wav`` produces 24 kHz mono), so they
    concatenate directly; a mismatch raises rather than producing garbage. The append
    happens before normalization, so the chime is loudness-matched to the episode."""

    body, body_rate = _load_wav(combined_path)
    clip, clip_rate = _load_wav(clip_path)
    if clip_rate != body_rate:
        raise AudioError(f"chime sample rate {clip_rate} != episode {body_rate}")
    if clip.size(0) != body.size(0):
        raise AudioError(f"chime has {clip.size(0)} channels but episode has {body.size(0)}")
    pad_n = round(lead_silence_ms * body_rate / 1000)
    gap = torch.zeros((body.size(0), pad_n), dtype=body.dtype)
    _save_wav(combined_path, torch.cat([body, gap, clip], dim=1), body_rate)


def _load_wav(path: Path) -> tuple[torch.Tensor, int]:
    """Read a WAV via soundfile and return (channels, samples) float32 tensor.

    soundfile returns samples as (n,) for mono or (n, channels) for multi-
    channel; we transpose to torchaudio's (channels, samples) convention.
    """

    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    # ``data`` is (samples, channels); transpose to (channels, samples).
    tensor = torch.from_numpy(np.ascontiguousarray(data.T))
    return tensor, int(rate)


def _save_wav(path: Path, waveform: torch.Tensor, sample_rate: int) -> None:
    """Write a (channels, samples) float32 tensor as a 16-bit PCM WAV."""

    arr = waveform.detach().cpu().numpy()
    # soundfile expects (samples, channels); transpose back.
    sf.write(str(path), arr.T, sample_rate, subtype="PCM_16")


# --- Stage 4 + 5: normalize + MP3 encode ------------------------------------


# loudnorm targets get filled in from settings at call time so operators
# can tune the LUFS / true-peak / LRA values via env without a rebuild.
_PRE_LOUDNORM_FILTERS = (
    "agate=threshold=-25dB:ratio=1.4:attack=10:release=250",
    "afftdn=nf=-70",
    "acompressor=threshold=-20dB:ratio=2:attack=80:release=200:makeup=1dB",
)
# Single-pass (dynamic) form: the fallback when the measurement pass fails.
# Dynamic loudnorm rides program material and undershoots on peaky input, which
# is why the measured two-pass form below is the primary path.
_LOUDNORM_DYNAMIC = "loudnorm=I={lufs}:TP={tp}:LRA={lra}"
# Two-pass form: a constant gain computed from the measured values, with a
# peak limiter at the end of the chain. loudnorm's own linear mode cannot be
# used here: TTS speech is peaky enough that the true-peak ceiling clamps its
# gain (measured 2-4 LU under target in production), and the post-loudnorm EQ
# boosts would push peaks past the ceiling anyway. Gain first, EQ, then a
# limiter last caps what the EQ added and lands on target.
_LOUDNORM_GAIN = "volume={gain_db:.2f}dB"
_LOUDNORM_LIMITER = "alimiter=limit={limit:.6f}:attack=2:release=25:level=false"
_POST_LOUDNORM_FILTERS = (
    "equalizer=f=150:t=q:w=2:g=1",
    "equalizer=f=250:t=q:w=2:g=-3",
    "equalizer=f=3000:t=q:w=2:g=2",
    "equalizer=f=5500:t=q:w=2:g=-4",
    "equalizer=f=9000:t=q:w=2:g=-2",
    "highpass=f=63",
)


def _measure_loudness(input_wav: Path, settings: Settings) -> dict[str, float] | None:
    """First loudnorm pass: measure the input as the loudnorm filter will see
    it (after the gate/denoise/compressor stages) and return the measured_*
    values for the linear second pass. Returns None on any failure -- the
    caller falls back to single-pass, so this can never fail an episode."""

    loudnorm = _LOUDNORM_DYNAMIC.format(
        lufs=settings.LOUDNORM_TARGET_LUFS,
        tp=settings.LOUDNORM_TRUE_PEAK_DB,
        lra=settings.LOUDNORM_LRA,
    )
    filters = ",".join([*_PRE_LOUDNORM_FILTERS, loudnorm + ":print_format=json"])
    cmd = [
        "ffmpeg",
        "-i",
        str(input_wav),
        "-af",
        filters,
        "-f",
        "null",
        "-",
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        logger.warning(
            "loudnorm measurement pass failed; falling back to single-pass",
            extra={"event": "audio_loudnorm_measure_failed", "code": completed.returncode},
        )
        return None
    start, end = completed.stderr.rfind("{"), completed.stderr.rfind("}")
    try:
        stats = json.loads(completed.stderr[start : end + 1])
        measured = {
            "measured_i": float(stats["input_i"]),
            "measured_tp": float(stats["input_tp"]),
            "measured_lra": float(stats["input_lra"]),
            "measured_thresh": float(stats["input_thresh"]),
            "offset": float(stats["target_offset"]),
        }
    except (ValueError, KeyError, TypeError):
        logger.warning(
            "loudnorm measurement output unparseable; falling back to single-pass",
            extra={"event": "audio_loudnorm_measure_failed", "code": 0},
        )
        return None
    # A fully silent input measures -inf; linear mode can't use that.
    if not all(math.isfinite(v) for v in measured.values()):
        logger.warning(
            "loudnorm measurement non-finite; falling back to single-pass",
            extra={"event": "audio_loudnorm_measure_failed", "code": 0},
        )
        return None
    return measured


def normalize_and_encode(
    input_wav: Path,
    output_mp3: Path,
    settings: Settings,
) -> EncodeResult:
    """Run the build-plan ffmpeg filter chain and encode to MP3.

    Mono->stereo upmix via ``-ac 2`` matches podcast-client expectations.
    Final duration comes from mutagen reading the MP3 header.
    """

    output_mp3.parent.mkdir(parents=True, exist_ok=True)
    measured = _measure_loudness(input_wav, settings)
    if measured:
        gain_db = settings.LOUDNORM_TARGET_LUFS - measured["measured_i"] + measured["offset"]
        stages = [
            *_PRE_LOUDNORM_FILTERS,
            _LOUDNORM_GAIN.format(gain_db=gain_db),
            *_POST_LOUDNORM_FILTERS,
            _LOUDNORM_LIMITER.format(limit=10 ** (settings.LOUDNORM_TRUE_PEAK_DB / 20)),
        ]
    else:
        gain_db = None
        stages = [
            *_PRE_LOUDNORM_FILTERS,
            _LOUDNORM_DYNAMIC.format(
                lufs=settings.LOUDNORM_TARGET_LUFS,
                tp=settings.LOUDNORM_TRUE_PEAK_DB,
                lra=settings.LOUDNORM_LRA,
            ),
            *_POST_LOUDNORM_FILTERS,
        ]
    filters = ",".join(stages)

    cmd = [
        "ffmpeg",
        "-y",  # overwrite existing MP3 on reprocess
        "-loglevel",
        "error",
        "-i",
        str(input_wav),
        "-af",
        filters,
        "-c:a",
        "libmp3lame",
        "-b:a",
        settings.MP3_BITRATE,
        "-ar",
        str(settings.MP3_SAMPLE_RATE),
        "-ac",
        str(settings.MP3_CHANNELS),
        str(output_mp3),
    ]

    logger.info(
        "Running ffmpeg normalize + encode",
        extra={
            "event": "audio_encode_start",
            "input": str(input_wav),
            "output": str(output_mp3),
            "loudnorm_mode": "two_pass_gain" if measured else "single_pass_dynamic",
            "loudnorm_gain_db": round(gain_db, 2) if gain_db is not None else None,
            "mp3_bitrate": settings.MP3_BITRATE,
            "mp3_sample_rate": settings.MP3_SAMPLE_RATE,
            "mp3_channels": settings.MP3_CHANNELS,
        },
    )
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise FfmpegError(completed.returncode, completed.stderr)

    duration = _read_mp3_duration(output_mp3)
    logger.info(
        "ffmpeg encode complete",
        extra={
            "event": "audio_encode_done",
            "output": str(output_mp3),
            "duration_secs": duration,
        },
    )
    return EncodeResult(mp3_path=output_mp3, duration_secs=duration)


def _read_mp3_duration(path: Path) -> float:
    info = MP3(str(path)).info
    return float(info.length)


# --- Cleanup helper ---------------------------------------------------------


def remove_quietly(*paths: Path) -> None:
    """Delete each path, swallowing FileNotFoundError.

    Used by the pipeline's audio stage ``finally`` block to clean up per-chunk
    WAVs and the concatenated WAV regardless of whether the encode succeeded.
    """

    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(
                "Failed to remove intermediate audio file",
                extra={
                    "event": "audio_cleanup_failed",
                    "path": str(path),
                    "error": str(exc),
                },
            )
