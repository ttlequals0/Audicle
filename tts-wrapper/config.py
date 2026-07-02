"""Wrapper-side configuration.

Structural container settings only (device, paths, whisper capability), loaded
from environment variables. Generation tuning (temperature, repetition_penalty,
top_p, top_k, seed, max_chars) is NOT configured here: the backend sends those
knobs on every ``/generate`` request (``engine.GenerationParams``), sourced
from its operator-tunable runtime settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

NUM_SLOTS = 5  # reference-voice slots; matches the backend's voices.NUM_SLOTS


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

    sample_rate: int  # provisional rate; replaced with the model's own sr at load

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
            sample_rate=_int_env("TTS_SAMPLE_RATE", 24000),
            whisper_enabled=_bool_env("WHISPER_ENABLED", False),
            whisper_model=_str_env("WHISPER_MODEL", "base"),
            whisper_device=whisper_device,
            whisper_compute_type=_str_env(
                "WHISPER_COMPUTE_TYPE", "float16" if whisper_device == "cuda" else "int8"
            ),
        )

    def slot_path(self, slot: int) -> Path:
        """Path to reference-voice slot ``slot``, under ``voices/`` next to
        ``reference_path`` (the read-only mount the backend writes slots into).
        Single source of the slot layout for the wrapper (boot pick + /select-voice)."""

        return Path(self.reference_path).parent / "voices" / f"slot{slot}.wav"
