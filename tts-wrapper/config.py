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

    # Engine selector: "chatterbox" (default) | "xtts" | "styletts2". XTTS and
    # Chatterbox are text-only; StyleTTS2 phonemizes via gruut and can honor
    # injected IPA (the `pronunciations` map). Must match the image's installed
    # backend (Dockerfile TTS_BACKEND) -- engines are not co-installed.
    engine: str
    # StyleTTS2-only knobs (ignored by XTTS/Chatterbox). model_path empty -> the
    # package's bundled default; phonemizer_lang feeds gruut.
    style_model_path: str
    style_phonemizer_lang: str

    # Chatterbox-only generate knobs (ignored by XTTS/StyleTTS2). exaggeration is
    # baked into the reference conditionals at load; cfg_weight/temperature apply
    # per call. temperature is below Turbo's 0.8 to cut sampling variance (the
    # "right dozens of times then wrong once" mispronunciation). seed makes a
    # generation reproducible; 0 disables seeding (prior random behavior).
    chatterbox_exaggeration: float
    chatterbox_cfg_weight: float
    chatterbox_temperature: float
    chatterbox_seed: int

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
            engine=os.environ.get("TTS_ENGINE", "chatterbox"),
            style_model_path=os.environ.get("STYLETTS2_MODEL_PATH", ""),
            style_phonemizer_lang=os.environ.get("STYLETTS2_PHONEMIZER_LANG", "en-us"),
            # exaggeration 0.0 + cfg_weight 0.0 == neutral read; temperature 0.5
            # (down from Turbo's 0.8) trims sampling variance; seed 1234 makes a
            # chunk reproducible (set 0 to disable).
            chatterbox_exaggeration=_float_env("CHATTERBOX_EXAGGERATION", 0.0),
            chatterbox_cfg_weight=_float_env("CHATTERBOX_CFG_WEIGHT", 0.0),
            chatterbox_temperature=_float_env("CHATTERBOX_TEMPERATURE", 0.5),
            chatterbox_seed=_int_env("CHATTERBOX_SEED", 1234),
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
            whisper_enabled=_bool_env("WHISPER_ENABLED", False),
            whisper_model=_str_env("WHISPER_MODEL", "base"),
            whisper_device=whisper_device,
            whisper_compute_type=_str_env(
                "WHISPER_COMPUTE_TYPE", "float16" if whisper_device == "cuda" else "int8"
            ),
        )
