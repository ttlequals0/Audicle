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

    # Engine selector: "chatterbox" (default) | "xtts" | "styletts2". XTTS and
    # Chatterbox are text-only; StyleTTS2 phonemizes via gruut and can honor
    # injected IPA (the `pronunciations` map). Must match the image's installed
    # backend (Dockerfile TTS_BACKEND) -- engines are not co-installed.
    engine: str
    # StyleTTS2-only knobs (ignored by XTTS/Chatterbox). model_path empty -> the
    # package's bundled default; phonemizer_lang feeds gruut.
    style_model_path: str
    style_phonemizer_lang: str

    # Chatterbox-only generate knobs (ignored by XTTS/StyleTTS2). Defaults match
    # Chatterbox Turbo's own neutral straight read; exaggeration is baked into the
    # reference conditionals at load, cfg_weight/temperature apply per call.
    chatterbox_exaggeration: float
    chatterbox_cfg_weight: float
    chatterbox_temperature: float

    temperature: float
    length_penalty: float
    repetition_penalty: float
    top_k: int
    top_p: float

    # Speaker-conditioning controls for get_conditioning_latents(). Defaults match
    # XTTS-v2's own (gpt_cond_len 6, gpt_cond_chunk_len 6, max_ref_length 30), so
    # leaving them unset reproduces prior behavior. Raising gpt_cond_len consumes
    # more of a long reference clip, which steadies prosody and reduces the
    # per-piece pitch drift; tune by ear, since too much can blur the voice.
    gpt_cond_len: int
    gpt_cond_chunk_len: int
    max_ref_length: int

    speed: float  # inference() playback speed; <1.0 slows (counters speed-up drift)

    sample_rate: int  # XTTS-v2 native output rate
    max_chars: int  # per-piece cap fed to inference(); kept under XTTS's ~250 warn

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            device=os.environ.get("TTS_DEVICE", "cuda"),
            language=os.environ.get("TTS_LANGUAGE", "en"),
            reference_path=os.environ.get("TTS_REFERENCE_PATH", "/app/reference/voice.wav"),
            data_dir=os.environ.get("DATA_DIR", "/data"),
            engine=os.environ.get("TTS_ENGINE", "chatterbox"),
            style_model_path=os.environ.get("STYLETTS2_MODEL_PATH", ""),
            style_phonemizer_lang=os.environ.get("STYLETTS2_PHONEMIZER_LANG", "en-us"),
            # Turbo defaults: exaggeration 0.0 + cfg_weight 0.0 == neutral read.
            chatterbox_exaggeration=_float_env("CHATTERBOX_EXAGGERATION", 0.0),
            chatterbox_cfg_weight=_float_env("CHATTERBOX_CFG_WEIGHT", 0.0),
            chatterbox_temperature=_float_env("CHATTERBOX_TEMPERATURE", 0.8),
            # 0.60 (down from 0.65) trims the sampling variance that drives
            # per-piece pitch drift and the occasional hallucinated word.
            temperature=_float_env("XTTS_TEMPERATURE", 0.60),
            length_penalty=_float_env("XTTS_LENGTH_PENALTY", 1.0),
            repetition_penalty=_float_env("XTTS_REPETITION_PENALTY", 2.0),
            top_k=_int_env("XTTS_TOP_K", 50),
            top_p=_float_env("XTTS_TOP_P", 0.85),
            gpt_cond_len=_int_env("XTTS_GPT_COND_LEN", 6),
            gpt_cond_chunk_len=_int_env("XTTS_GPT_COND_CHUNK_LEN", 6),
            max_ref_length=_int_env("XTTS_MAX_REF_LENGTH", 30),
            speed=_float_env("XTTS_SPEED", 1.0),
            sample_rate=_int_env("XTTS_SAMPLE_RATE", 24000),
            max_chars=_int_env("XTTS_MAX_CHARS", 200),
        )
