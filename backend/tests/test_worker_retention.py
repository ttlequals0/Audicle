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
