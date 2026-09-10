"""Content-addressed cache of synthesized TTS chunks.

A chunk's synthesized audio is deterministic-enough-to-reuse when backend,
model, voice, language, text, and generation params match. Every hit still
runs the current quality policy because thresholds and pitch context are not
part of the synthesis identity.

Storage: ``<data_dir>/tts_cache/<key>.wav`` + ``<key>.json`` (duration_secs,
sample_rate, transcript, qa_passed, median_f0_hz). The cache is best-effort:
every failure here must be swallowed by the caller and treated as a miss,
never as a chunk failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.services import voices
from app.services.atomic_write import write_bytes_atomic


@dataclass(frozen=True)
class CachedChunk:
    wav_path: Path
    """Path inside the cache dir; the caller copies it to the canonical
    per-chunk path rather than reusing it in place."""
    duration_secs: float
    sample_rate: int
    transcript: str | None
    qa_passed: bool = False
    """Whether the stored take passed the policy active when it was written.
    Retained for backward-compatible metadata; callers rerun current checks."""
    median_f0_hz: float | None = None
    """Median F0 measured when stored. Current policy recomputes it because
    pitch acceptance depends on preceding chunks."""


def cache_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "tts_cache"


def cache_key(
    *,
    backend: str,
    model: str,
    voice_fingerprint: str,
    language: str,
    text: str,
    params: dict[str, Any],
) -> str:
    """sha256 hex of a sorted-json payload over everything that determines the
    synthesized audio. ``params`` is the BASELINE ``tts.generation_params``
    (including the configured seed) -- the identity of "what these settings
    would synthesize", independent of which regen attempt actually produced
    the cached take."""

    payload = {
        "backend": backend,
        "model": model,
        "voice_fingerprint": voice_fingerprint,
        "language": language,
        "text": text,
        "params": params,
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def lookup(data_dir: Path, key: str) -> CachedChunk | None:
    """Both the wav and json must exist; a malformed json is treated as
    absent and the (possibly partial) pair is best-effort removed."""

    d = cache_dir(data_dir)
    wav_path = d / f"{key}.wav"
    json_path = d / f"{key}.json"
    if not wav_path.is_file():
        return None
    try:
        with wave.open(str(wav_path), "rb") as handle:
            if handle.getcomptype() != "NONE" or handle.getsampwidth() not in {1, 2, 3, 4}:
                raise ValueError("cached audio is not uncompressed PCM")
        payload = json.loads(json_path.read_text())
        duration_secs = float(payload["duration_secs"])
        sample_rate = int(payload["sample_rate"])
        raw_transcript = payload.get("transcript")
        transcript = str(raw_transcript) if raw_transcript is not None else None
        qa_passed = bool(payload.get("qa_passed", False))
        raw_f0 = payload.get("median_f0_hz")
        median_f0_hz = float(raw_f0) if raw_f0 is not None else None
    except (OSError, EOFError, ValueError, TypeError, KeyError, wave.Error):
        wav_path.unlink(missing_ok=True)
        json_path.unlink(missing_ok=True)
        return None
    return CachedChunk(
        wav_path=wav_path,
        duration_secs=duration_secs,
        sample_rate=sample_rate,
        transcript=transcript,
        qa_passed=qa_passed,
        median_f0_hz=median_f0_hz,
    )


def link_or_copy(src: str | Path, dst: str | Path) -> None:
    """Hard-link ``dst`` to ``src``, falling back to a full copy when the two
    sit on different filesystems. Both cache and media dirs normally live
    under DATA_DIR, so the link (metadata-only, no byte copy) is the common
    case; chunk WAVs are never modified in place, so sharing the inode is
    safe. An existing ``dst`` is replaced either way.
    """

    dst = Path(dst)
    dst.unlink(missing_ok=True)
    try:
        os.link(str(src), str(dst))
    except OSError:
        shutil.copyfile(str(src), str(dst))


def store(
    data_dir: Path,
    key: str,
    wav_path: str | Path,
    duration_secs: float,
    sample_rate: int,
    transcript: str | None,
    *,
    qa_passed: bool,
    median_f0_hz: float | None = None,
) -> None:
    d = cache_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    link_or_copy(wav_path, d / f"{key}.wav")
    payload = {
        "duration_secs": duration_secs,
        "sample_rate": sample_rate,
        "transcript": transcript,
        "qa_passed": qa_passed,
        "median_f0_hz": median_f0_hz,
    }
    write_bytes_atomic(d / f"{key}.json", json.dumps(payload).encode("utf-8"))


def purge_older_than(data_dir: Path, days: int) -> int:
    """Remove cache entries whose wav is older than ``days``. Returns the
    number of entries removed."""

    d = cache_dir(data_dir)
    if not d.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for wav_path in d.glob("*.wav"):
        try:
            mtime = wav_path.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        wav_path.unlink(missing_ok=True)
        wav_path.with_suffix(".json").unlink(missing_ok=True)
        removed += 1
    return removed


def voice_fingerprint_for_slot(slot: int) -> str | None:
    """sha256 of the reference-voice WAV bytes for ``slot``, or ``None`` when
    the slot file is missing/unreadable. The hash itself is cached on
    (path, mtime, size) so an unchanged reference file is not re-read and
    re-hashed once per chunk over a long episode; only one stat call is
    paid on a cache hit."""

    path = voices.slot_path(slot)
    try:
        stat = path.stat()
    except OSError:
        return None
    return _hash_file(path, stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=32)
def _hash_file(path: Path, mtime_ns: int, size: int) -> str:
    # Keyed on (path, mtime_ns, size): a same-nanosecond replacement that
    # also lands on the same byte size is the one theoretical staleness
    # window, well below the granularity a manual voice re-upload can hit.
    return hashlib.sha256(path.read_bytes()).hexdigest()
