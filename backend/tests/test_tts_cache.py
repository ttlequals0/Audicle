from __future__ import annotations

import json
import os
import time
from pathlib import Path

from app.services import tts_cache


def _base_key(**overrides) -> str:
    kwargs = {
        "backend": "wrapper",
        "model": "chatterbox-turbo",
        "voice_fingerprint": "abc123",
        "language": "en",
        "text": "hello world",
        "params": {"temperature": 0.5, "seed": 1234},
    }
    kwargs.update(overrides)
    return tts_cache.cache_key(**kwargs)


def test_cache_key_stable_for_same_inputs() -> None:
    assert _base_key() == _base_key()


def test_cache_key_changes_with_text() -> None:
    assert _base_key() != _base_key(text="goodbye world")


def test_cache_key_changes_with_params() -> None:
    assert _base_key() != _base_key(params={"temperature": 0.9, "seed": 1234})


def test_cache_key_changes_with_voice_fingerprint() -> None:
    assert _base_key() != _base_key(voice_fingerprint="different")


def test_store_and_lookup_round_trip(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    src_wav = tmp_path / "src.wav"
    src_wav.write_bytes(b"RIFF-fake-wav-bytes")
    key = _base_key()

    tts_cache.store(data_dir, key, src_wav, duration_secs=1.5, sample_rate=24000, transcript="hi")

    cached = tts_cache.lookup(data_dir, key)
    assert cached is not None
    assert cached.wav_path.read_bytes() == b"RIFF-fake-wav-bytes"
    assert cached.duration_secs == 1.5
    assert cached.sample_rate == 24000
    assert cached.transcript == "hi"


def test_store_and_lookup_round_trip_no_transcript(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    src_wav = tmp_path / "src.wav"
    src_wav.write_bytes(b"bytes")
    key = _base_key()

    tts_cache.store(data_dir, key, src_wav, duration_secs=2.0, sample_rate=22050, transcript=None)

    cached = tts_cache.lookup(data_dir, key)
    assert cached is not None
    assert cached.transcript is None


def test_lookup_missing_json_returns_none(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    key = "somekey"
    cache_dir = tts_cache.cache_dir(data_dir)
    cache_dir.mkdir(parents=True)
    (cache_dir / f"{key}.wav").write_bytes(b"wav-only")

    assert tts_cache.lookup(data_dir, key) is None


def test_lookup_missing_wav_returns_none(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    key = "somekey"
    cache_dir = tts_cache.cache_dir(data_dir)
    cache_dir.mkdir(parents=True)
    (cache_dir / f"{key}.json").write_text(
        json.dumps({"duration_secs": 1.0, "sample_rate": 24000, "transcript": None})
    )

    assert tts_cache.lookup(data_dir, key) is None


def test_lookup_malformed_json_returns_none_and_cleans_up(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    key = "somekey"
    cache_dir = tts_cache.cache_dir(data_dir)
    cache_dir.mkdir(parents=True)
    wav_path = cache_dir / f"{key}.wav"
    json_path = cache_dir / f"{key}.json"
    wav_path.write_bytes(b"wav-bytes")
    json_path.write_text("not-json{{{")

    assert tts_cache.lookup(data_dir, key) is None
    assert not wav_path.exists()
    assert not json_path.exists()


def test_purge_older_than_removes_only_old_entries(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    old_wav = tmp_path / "old.wav"
    new_wav = tmp_path / "new.wav"
    old_wav.write_bytes(b"old")
    new_wav.write_bytes(b"new")

    tts_cache.store(data_dir, "old_key", old_wav, duration_secs=1.0, sample_rate=24000, transcript=None)
    tts_cache.store(data_dir, "new_key", new_wav, duration_secs=1.0, sample_rate=24000, transcript=None)

    cache_dir = tts_cache.cache_dir(data_dir)
    old_time = time.time() - (10 * 86400)
    for suffix in (".wav", ".json"):
        os.utime(cache_dir / f"old_key{suffix}", (old_time, old_time))

    removed = tts_cache.purge_older_than(data_dir, days=7)

    assert removed == 1
    assert tts_cache.lookup(data_dir, "old_key") is None
    assert tts_cache.lookup(data_dir, "new_key") is not None


def test_voice_fingerprint_for_slot_missing_returns_none(tmp_path: Path, monkeypatch) -> None:
    from app.services import voices

    d = tmp_path / "voices"
    d.mkdir()
    monkeypatch.setattr(voices, "voices_dir", lambda: d)

    assert tts_cache.voice_fingerprint_for_slot(1) is None


def test_voice_fingerprint_for_slot_returns_sha256_of_bytes(tmp_path: Path, monkeypatch) -> None:
    import hashlib

    from app.services import voices

    d = tmp_path / "voices"
    d.mkdir()
    (d / "slot1.wav").write_bytes(b"reference-voice-bytes")
    monkeypatch.setattr(voices, "voices_dir", lambda: d)

    expected = hashlib.sha256(b"reference-voice-bytes").hexdigest()
    assert tts_cache.voice_fingerprint_for_slot(1) == expected
