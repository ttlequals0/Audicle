from __future__ import annotations

import io
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
import torch
from app.config import get_settings
from app.services import audio

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg binary not available in test environment",
)


def _save_for_test(path: Path, waveform: torch.Tensor, sample_rate: int) -> None:
    arr = waveform.detach().cpu().numpy().T
    sf.write(str(path), arr, sample_rate, subtype="PCM_16")


def _load_for_test(path: Path) -> tuple[torch.Tensor, int]:
    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    return torch.from_numpy(np.ascontiguousarray(data.T)), int(rate)


def _write_silent_wav(path: Path, *, duration_secs: float, sample_rate: int = 24000) -> None:
    n = round(duration_secs * sample_rate)
    waveform = torch.zeros((1, n))
    _save_for_test(path, waveform, sample_rate)


def _write_tone_wav(
    path: Path, *, duration_secs: float, sample_rate: int = 24000, freq: float = 440.0
) -> None:
    n = round(duration_secs * sample_rate)
    t = torch.arange(n, dtype=torch.float32) / sample_rate
    waveform = (0.5 * torch.sin(2 * torch.pi * freq * t)).unsqueeze(0)
    _save_for_test(path, waveform, sample_rate)


# --- trim_silence ----------------------------------------------------------


def test_trim_silence_removes_leading_and_trailing_silence(env: Path) -> None:
    sample_rate = 24000
    silence = torch.zeros((1, sample_rate // 2))  # 0.5s
    tone = 0.5 * torch.ones((1, sample_rate))  # 1s flat tone
    waveform = torch.cat([silence, tone, silence], dim=1)

    trimmed = audio.trim_silence(waveform, sample_rate, get_settings())
    # Trimmed should be close to 1s + small buffer; allow generous margin.
    assert sample_rate * 0.9 <= trimmed.size(1) <= sample_rate * 1.1


def test_trim_silence_keeps_fully_silent_input(env: Path) -> None:
    """If every sample is silent, trim returns the original waveform rather
    than emitting an empty tensor (which would crash torchaudio.save)."""

    sample_rate = 24000
    waveform = torch.zeros((1, sample_rate))
    trimmed = audio.trim_silence(waveform, sample_rate, get_settings())
    assert trimmed.size() == waveform.size()


# --- concat_with_padding ---------------------------------------------------


def test_concat_with_padding_appends_inter_chunk_silence(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TTS_CHUNK_SILENCE_MS", "100")  # 0.1s padding for fast test
    get_settings.cache_clear()
    sample_rate = 24000

    chunk_a = tmp_path / "a.wav"
    chunk_b = tmp_path / "b.wav"
    _write_tone_wav(chunk_a, duration_secs=0.3)
    _write_tone_wav(chunk_b, duration_secs=0.2)

    out = tmp_path / "combined.wav"
    result_path, result_rate = audio.concat_with_padding([chunk_a, chunk_b], out, get_settings())
    assert result_path == out
    assert result_rate == sample_rate
    waveform, rate = _load_for_test(out)
    assert rate == sample_rate
    # 0.3 (tone) + 0.1 (silence padding) + 0.2 (tone) = 0.6s; allow
    # silence-trim margin (~5ms each side).
    expected_samples = int(0.6 * sample_rate)
    assert abs(waveform.size(1) - expected_samples) < int(0.05 * sample_rate)


def test_concat_with_padding_rejects_zero_chunks(tmp_path: Path, env: Path) -> None:
    with pytest.raises(audio.AudioError, match="zero chunks"):
        audio.concat_with_padding([], tmp_path / "out.wav", get_settings())


def test_concat_with_padding_rejects_rate_mismatch(tmp_path: Path, env: Path) -> None:
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    _write_tone_wav(a, duration_secs=0.1, sample_rate=24000)
    _write_tone_wav(b, duration_secs=0.1, sample_rate=22050)

    with pytest.raises(audio.AudioError, match="sample rate"):
        audio.concat_with_padding([a, b], tmp_path / "out.wav", get_settings())


# --- normalize_and_encode (real ffmpeg) ------------------------------------


def test_normalize_and_encode_produces_valid_mp3(env: Path, tmp_path: Path) -> None:
    src_wav = tmp_path / "in.wav"
    out_mp3 = tmp_path / "out.mp3"
    _write_tone_wav(src_wav, duration_secs=0.5)

    result = audio.normalize_and_encode(src_wav, out_mp3, get_settings())
    assert result.mp3_path == out_mp3
    assert out_mp3.exists()
    # mutagen-read duration should be close to the 0.5s input.
    assert 0.3 <= result.duration_secs <= 1.0


def test_normalize_and_encode_raises_ffmpeg_error_on_missing_input(
    env: Path, tmp_path: Path
) -> None:
    src_wav = tmp_path / "does_not_exist.wav"
    out_mp3 = tmp_path / "out.mp3"
    with pytest.raises(audio.FfmpegError):
        audio.normalize_and_encode(src_wav, out_mp3, get_settings())


# --- remove_quietly --------------------------------------------------------


def test_remove_quietly_swallows_missing(tmp_path: Path) -> None:
    existing = tmp_path / "x.wav"
    existing.write_bytes(b"hi")
    missing = tmp_path / "nope.wav"

    audio.remove_quietly(existing, missing)
    assert not existing.exists()
    assert not missing.exists()


# --- /generate WAV header smoke check via low-level wave module ------------


def test_wav_round_trip_via_wave_module(tmp_path: Path, env: Path) -> None:
    """Sanity: torchaudio writes a 24000 Hz mono WAV header readable by stdlib
    wave; the audio pipeline reads its own output that way during stitching."""

    path = tmp_path / "rt.wav"
    _write_silent_wav(path, duration_secs=0.25)
    with wave.open(str(path), "rb") as wf:
        assert wf.getframerate() == 24000
        assert wf.getnchannels() == 1


def test_concat_with_padding_rejects_channel_mismatch(env: Path, tmp_path: Path) -> None:
    """A mid-stream channel-count change between chunks must surface as a
    clean AudioError -- raw torch.cat would otherwise raise an opaque
    'Sizes of tensors must match except in dimension 1' that's useless to
    operators."""

    import numpy as np
    import soundfile as sf

    mono = tmp_path / "mono.wav"
    stereo = tmp_path / "stereo.wav"
    sf.write(str(mono), np.zeros((1024, 1), dtype="float32"), 24000, subtype="PCM_16")
    sf.write(str(stereo), np.zeros((1024, 2), dtype="float32"), 24000, subtype="PCM_16")

    with pytest.raises(audio.AudioError, match="channels"):
        audio.concat_with_padding([mono, stereo], tmp_path / "out.wav", get_settings())


def test_transcode_to_wav_decodes_mp3(tmp_path: Path) -> None:
    src = tmp_path / "in.wav"
    _write_silent_wav(src, duration_secs=10.0)
    mp3 = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-c:a", "libmp3lame", "-f", "mp3", "pipe:1"],
        capture_output=True,
        check=True,
    ).stdout
    assert not mp3.startswith(b"RIFF")  # it really is mp3, not wav

    wav = audio.transcode_to_wav(mp3)
    assert wav.startswith(b"RIFF")
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getframerate() == 24000
        secs = w.getnframes() / w.getframerate()
    assert 9.0 <= secs <= 11.0


def test_transcode_to_wav_rejects_garbage() -> None:
    with pytest.raises(audio.FfmpegError):
        audio.transcode_to_wav(b"this is not audio")
