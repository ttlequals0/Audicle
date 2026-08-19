from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from app import worker
from app.config import get_settings
from app.core import database
from app.services import episodes


def _freeze_now(monkeypatch: pytest.MonkeyPatch, fake_now: datetime) -> None:
    """Replace ``worker.datetime`` with a class whose ``now`` returns
    ``fake_now`` but whose remaining surface delegates to the real datetime.

    Wrapping (rather than only stubbing ``now``) means a future change in
    ``worker.py`` that adds ``datetime.fromisoformat(...)`` or constructs a
    new ``datetime(...)`` doesn't break every test in this file with an
    AttributeError.
    """

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    # Keep the original behavior of every classmethod/staticmethod except now.
    monkeypatch.setattr(worker, "datetime", _Frozen)


def _seed_old(env: Path, *, id_: str) -> None:
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id=id_,
            job_id=None,
            original_url=f"https://example.test/{id_}",
            title=id_,
            author="A",
            audio_path=f"/data/media/{id_}.mp3",
            artwork_path=None,
            transcript_vtt=None,
            duration_secs=10,
        )
        conn.execute(
            "UPDATE episodes SET pub_date='2020-01-01T00:00:00Z' WHERE id=?",
            (id_,),
        )
        conn.commit()
    finally:
        conn.close()


def test_maybe_run_retention_sweep_runs_when_hour_matches(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the current UTC hour equals RETENTION_SWEEP_HOUR_UTC and we
    haven't already run today, the sweep fires and the function returns
    today's date so subsequent calls skip."""

    settings = get_settings()
    fake_now = datetime(2026, 5, 28, settings.RETENTION_SWEEP_HOUR_UTC, 30, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fake_now)
    _seed_old(env, id_="old")

    new_day = worker._maybe_run_retention_sweep(settings, last_sweep_day=None)
    assert new_day == "2026-05-28"

    conn = database.connect(database.db_path(env))
    try:
        assert episodes.get_by_id(conn, "old") is None
    finally:
        conn.close()


def test_maybe_run_retention_sweep_keeps_all_when_days_zero(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RETENTION_DAYS=0 means "keep forever": the automatic sweep must NOT
    purge. The wipe-all-on-zero contract is reserved for the explicit
    operator-initiated /purge endpoint."""

    monkeypatch.setenv("RETENTION_DAYS", "0")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.RETENTION_DAYS == 0
    fake_now = datetime(2026, 5, 28, settings.RETENTION_SWEEP_HOUR_UTC, 30, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fake_now)
    _seed_old(env, id_="old")

    new_day = worker._maybe_run_retention_sweep(settings, last_sweep_day=None)
    assert new_day == "2026-05-28"

    conn = database.connect(database.db_path(env))
    try:
        assert episodes.get_by_id(conn, "old") is not None
    finally:
        conn.close()


def test_maybe_run_retention_sweep_skips_when_already_ran_today(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    fake_now = datetime(2026, 5, 28, settings.RETENTION_SWEEP_HOUR_UTC, 30, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fake_now)
    _seed_old(env, id_="old")

    same = worker._maybe_run_retention_sweep(settings, last_sweep_day="2026-05-28")
    assert same == "2026-05-28"

    conn = database.connect(database.db_path(env))
    try:
        assert episodes.get_by_id(conn, "old") is not None
    finally:
        conn.close()


def test_maybe_run_retention_sweep_does_nothing_when_hour_does_not_match(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    off_hour = (settings.RETENTION_SWEEP_HOUR_UTC + 1) % 24
    fake_now = datetime(2026, 5, 28, off_hour, 30, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fake_now)
    _seed_old(env, id_="old")

    unchanged = worker._maybe_run_retention_sweep(settings, last_sweep_day=None)
    assert unchanged is None

    conn = database.connect(database.db_path(env))
    try:
        assert episodes.get_by_id(conn, "old") is not None
    finally:
        conn.close()


def test_maybe_run_retention_sweep_logs_and_returns_unchanged_on_failure(
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed sweep must not blow up the worker loop or mark the day as
    swept; the next iteration retries."""

    import logging

    from app.services import retention

    settings = get_settings()
    fake_now = datetime(2026, 5, 28, settings.RETENTION_SWEEP_HOUR_UTC, 30, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fake_now)

    def _boom(_settings, *, older_than_days):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(retention, "purge_older_than", _boom)
    with caplog.at_level(logging.ERROR, logger="app.worker"):
        result = worker._maybe_run_retention_sweep(settings, last_sweep_day=None)
    assert result is None
    assert any(getattr(rec, "event", "") == "retention_sweep_failed" for rec in caplog.records)


def test_maybe_run_retention_sweep_purges_old_tts_cache_entries(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep added for the resumable TTS chunk cache purges entries
    older than TTS_CACHE_RETENTION_DAYS, same as the other sweeps it runs
    alongside."""

    import os
    import time as time_mod

    from app.services import tts_cache

    database.run_migrations(env)
    settings = get_settings()
    fake_now = datetime(2026, 5, 28, settings.RETENTION_SWEEP_HOUR_UTC, 30, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fake_now)

    wav_src = env / "src.wav"
    wav_src.write_bytes(b"fake-wav-bytes")
    tts_cache.store(
        settings.DATA_DIR,
        "old_key",
        wav_src,
        duration_secs=1.0,
        sample_rate=24000,
        transcript=None,
        qa_passed=True,
    )
    cache_dir = tts_cache.cache_dir(settings.DATA_DIR)
    old_time = time_mod.time() - (settings.TTS_CACHE_RETENTION_DAYS + 1) * 86400
    for suffix in (".wav", ".json"):
        os.utime(cache_dir / f"old_key{suffix}", (old_time, old_time))

    worker._maybe_run_retention_sweep(settings, last_sweep_day=None)

    assert tts_cache.lookup(settings.DATA_DIR, "old_key") is None


def test_maybe_run_retention_sweep_logs_and_returns_unchanged_on_tts_cache_purge_failure(
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken tts_cache.purge_older_than must be caught the same way as
    the other sweep failures above, not just the retention.py ones."""

    import logging

    from app.services import tts_cache

    database.run_migrations(env)
    settings = get_settings()
    fake_now = datetime(2026, 5, 28, settings.RETENTION_SWEEP_HOUR_UTC, 30, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fake_now)

    def _boom(_data_dir, _days):
        raise RuntimeError("cache disk on fire")

    monkeypatch.setattr(tts_cache, "purge_older_than", _boom)
    with caplog.at_level(logging.ERROR, logger="app.worker"):
        result = worker._maybe_run_retention_sweep(settings, last_sweep_day=None)
    assert result is None
    assert any(getattr(rec, "event", "") == "retention_sweep_failed" for rec in caplog.records)


def test_maybe_run_retention_sweep_clamps_tts_cache_retention_days(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TTS_CACHE_RETENTION_DAYS has no pydantic Field constraint (bounds live
    only in RUNTIME_SETTING_BOUNDS), so the sweep must clamp an out-of-range
    env value itself before calling purge_older_than."""

    from app.services import tts_cache

    database.run_migrations(env)
    monkeypatch.setenv("TTS_CACHE_RETENTION_DAYS", "0")
    get_settings.cache_clear()
    settings = get_settings()
    fake_now = datetime(2026, 5, 28, settings.RETENTION_SWEEP_HOUR_UTC, 30, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fake_now)

    captured: dict[str, int] = {}

    def _capture(_data_dir, days):
        captured["days"] = days
        return 0

    monkeypatch.setattr(tts_cache, "purge_older_than", _capture)
    worker._maybe_run_retention_sweep(settings, last_sweep_day=None)
    assert captured["days"] == 1  # clamped to RUNTIME_SETTING_BOUNDS["TTS_CACHE_RETENTION_DAYS"]["ge"]
