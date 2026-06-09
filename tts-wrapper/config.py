"""Wrapper-side configuration.

Loaded from environment variables so the operator can tune generation params
without rebuilding the image. The Chatterbox generate knobs live under the
``CHATTERBOX_*`` env vars (exaggeration, cfg_weight, temperature, seed).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else float(raw)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else int(raw)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _str_env(name: str, default: str) -> str:
    # Treat an empty-string env var as "unset" (-> default), matching the typed
    # helpers above. A plain os.environ.get returns "", which for whisper model/
    # device would silently break the model load.
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else raw


@dataclass(frozen=True)
class Config:
    device: str  # cuda | cpu
    language: str
    reference_path: str  # absolute path inside the container
    data_dir: str  # writes WAVs under {data_dir}/media

    # Chatterbox generate knobs. exaggeration is baked into the reference
    # conditionals at load; cfg_weight/temperature apply per call. temperature is
    # below Turbo's 0.8 to cut sampling variance (the "right dozens of times then
    # wrong once" mispronunciation). seed makes a generation reproducible; 0
    # disables seeding (prior random behavior).
    chatterbox_exaggeration: float
    chatterbox_cfg_weight: float
    chatterbox_temperature: float
    chatterbox_seed: int

    sample_rate: int  # provisional rate; replaced with the model's own sr at load
    max_chars: int  # per-piece cap fed to the model before concatenation

    # Post-TTS ASR verification (off by default). When enabled, /generate
    # transcribes the produced audio with faster-whisper when the request asks
    # for it (verify=true), so the backend can diff that transcript against the
    # text it sent. compute_type follows device unless overridden (float16 on
    # cuda, int8 on cpu). The model downloads to the HF cache on first use.
    whisper_enabled: bool
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str

    @classmethod
    def from_env(cls) -> "Config":
        # ASR device defaults to the TTS device; compute_type then follows it
        # (float16 on GPU, int8 on CPU). Resolve once so the two can't drift.
        whisper_device = _str_env("WHISPER_DEVICE", _str_env("TTS_DEVICE", "cuda"))
        return cls(
            device=os.environ.get("TTS_DEVICE", "cuda"),
            language=os.environ.get("TTS_LANGUAGE", "en"),
            reference_path=os.environ.get("TTS_REFERENCE_PATH", "/app/reference/voice.wav"),
            data_dir=os.environ.get("DATA_DIR", "/data"),
            # exaggeration 0.0 + cfg_weight 0.0 == neutral read; temperature 0.5
            # (down from Turbo's 0.8) trims sampling variance; seed 1234 makes a
            # chunk reproducible (set 0 to disable).
            chatterbox_exaggeration=_float_env("CHATTERBOX_EXAGGERATION", 0.0),
            chatterbox_cfg_weight=_float_env("CHATTERBOX_CFG_WEIGHT", 0.0),
            chatterbox_temperature=_float_env("CHATTERBOX_TEMPERATURE", 0.5),
            chatterbox_seed=_int_env("CHATTERBOX_SEED", 1234),
            sample_rate=_int_env("TTS_SAMPLE_RATE", 24000),
            max_chars=_int_env("TTS_MAX_CHARS", 200),
            whisper_enabled=_bool_env("WHISPER_ENABLED", False),
            whisper_model=_str_env("WHISPER_MODEL", "base"),
            whisper_device=whisper_device,
            whisper_compute_type=_str_env(
                "WHISPER_COMPUTE_TYPE", "float16" if whisper_device == "cuda" else "int8"
            ),
        )
