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

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
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


# --- Stage 2 + 3: concat with silence padding -------------------------------


def concat_with_padding(
    chunk_paths: list[Path],
    output_path: Path,
    settings: Settings,
) -> tuple[Path, int]:
    """Load each chunk WAV, trim silence, append silence padding between
    chunks, write the concatenated WAV.

    Returns ``(output_path, sample_rate)`` so subsequent ffmpeg invocations
    can pin the sample rate explicitly. Padding is inserted *between* chunks
    only -- there's no leading or trailing pad.
    """

    if not chunk_paths:
        raise AudioError("concat_with_padding called with zero chunks")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    pieces: list[torch.Tensor] = []
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
    return output_path, sample_rate


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


_NORMALIZE_FILTERS = (
    "agate=threshold=-25dB:ratio=1.4:attack=10:release=250",
    "afftdn=nf=-70",
    "acompressor=threshold=-20dB:ratio=2:attack=80:release=200:makeup=1dB",
    # loudnorm targets get filled in from settings at call time so operators
    # can tune the LUFS / true-peak / LRA values via env without a rebuild.
    #
    # Note: ``linear=true`` is *not* set. The flag only takes effect when the
    # five measured_* params from a first-pass measurement are also supplied;
    # without them, ffmpeg silently uses dynamic single-pass loudnorm anyway,
    # so claiming ``linear=true`` would lie about what the filter is doing.
    # A two-pass implementation is a follow-up; for now we accept that the
    # output level rides program material around LOUDNORM_TARGET_LUFS.
    "loudnorm=I={lufs}:TP={tp}:LRA={lra}",
    "equalizer=f=150:t=q:w=2:g=1",
    "equalizer=f=250:t=q:w=2:g=-3",
    "equalizer=f=3000:t=q:w=2:g=2",
    "equalizer=f=5500:t=q:w=2:g=-4",
    "equalizer=f=9000:t=q:w=2:g=-2",
    "highpass=f=63",
)


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
    filters = ",".join(
        f.format(
            lufs=settings.LOUDNORM_TARGET_LUFS,
            tp=settings.LOUDNORM_TRUE_PEAK_DB,
            lra=settings.LOUDNORM_LRA,
        )
        for f in _NORMALIZE_FILTERS
    )

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
