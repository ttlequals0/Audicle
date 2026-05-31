"""Wrapper-side configuration.

Loaded from environment variables so the operator can tune generation params
without rebuilding the image.
"Generation parameters in config.py, tunable: temperature 0.60,
length_penalty 1.0, repetition_penalty 2.0, top_k 50, top_p 0.85".
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


@dataclass(frozen=True)
class Config:
    device: str  # cuda | cpu
    language: str
    reference_path: str  # absolute path inside the container
    data_dir: str  # writes WAVs under {data_dir}/media

    temperature: float
    length_penalty: float
    repetition_penalty: float
    top_k: int
    top_p: float

    sample_rate: int  # XTTS-v2 native output rate
    max_chars: int  # per-piece cap fed to inference(); kept under XTTS's ~250 warn

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            device=os.environ.get("TTS_DEVICE", "cuda"),
            language=os.environ.get("TTS_LANGUAGE", "en"),
            reference_path=os.environ.get("TTS_REFERENCE_PATH", "/app/reference/voice.wav"),
            data_dir=os.environ.get("DATA_DIR", "/data"),
            # 0.60 (down from 0.65) trims the sampling variance that drives
            # per-piece pitch drift and the occasional hallucinated word.
            temperature=_float_env("XTTS_TEMPERATURE", 0.60),
            length_penalty=_float_env("XTTS_LENGTH_PENALTY", 1.0),
            repetition_penalty=_float_env("XTTS_REPETITION_PENALTY", 2.0),
            top_k=_int_env("XTTS_TOP_K", 50),
            top_p=_float_env("XTTS_TOP_P", 0.85),
            sample_rate=_int_env("XTTS_SAMPLE_RATE", 24000),
            max_chars=_int_env("XTTS_MAX_CHARS", 200),
        )
