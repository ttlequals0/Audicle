from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from app.core import database


def test_run_migrations_creates_tables(tmp_path: Path) -> None:
    applied = database.run_migrations(tmp_path)
    assert applied == [
        "001_initial_schema",
        "002_settings_kv",
        "003_auth_lockout",
        "004_runtime_settings",
    ]

    conn = database.connect(database.db_path(tmp_path))
    try:
        names = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert {"jobs", "episodes"}.issubset(names)


def test_second_run_is_a_noop(tmp_path: Path) -> None:
    first = database.run_migrations(tmp_path)
    second = database.run_migrations(tmp_path)
    assert first == [
        "001_initial_schema",
        "002_settings_kv",
        "003_auth_lockout",
        "004_runtime_settings",
    ]
    assert second == []


def test_no_backup_on_fresh_init_or_noop(tmp_path: Path) -> None:
    database.run_migrations(tmp_path)
    database.run_migrations(tmp_path)
    assert sorted(tmp_path.glob(f"{database.BACKUP_PREFIX}*")) == []


def test_backup_when_pending_migration_runs_against_populated_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(tmp_path)
    conn = database.connect(database.db_path(tmp_path))
    try:
        conn.execute(
            "INSERT INTO jobs (id, url, episode_id, status) VALUES (?, ?, ?, ?)",
            ("j1", "https://x.test/a", "abc", "queued"),
        )
    finally:
        conn.close()

    def _m_fake(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS fake_table (id TEXT PRIMARY KEY)")

    monkeypatch.setattr(
        database,
        "MIGRATIONS",
        [*database.MIGRATIONS, ("002_fake", _m_fake)],
    )

    applied = database.run_migrations(tmp_path)
    assert applied == ["002_fake"]
    backups = sorted(tmp_path.glob(f"{database.BACKUP_PREFIX}*"))
    assert len(backups) == 1


def test_failing_migration_rolls_back_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration that raises mid-body must leave neither its DDL nor a
    schema_migrations row behind. Re-running picks the migration up cleanly."""

    database.run_migrations(tmp_path)

    raise_until = {"hits": 0}

    def _m_fails_once(conn: sqlite3.Connection) -> None:
        # The CREATE happens before the raise; the rollback should undo it.
        conn.execute("CREATE TABLE IF NOT EXISTS half_baked (id TEXT)")
        raise_until["hits"] += 1
        if raise_until["hits"] == 1:
            raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(
        database,
        "MIGRATIONS",
        [*database.MIGRATIONS, ("002_fails_once", _m_fails_once)],
    )

    with pytest.raises(RuntimeError, match="simulated"):
        database.run_migrations(tmp_path)

    conn = database.connect(database.db_path(tmp_path))
    try:
        # Migration body's CREATE was rolled back.
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='half_baked'"
            ).fetchone()
            is None
        )
        # No row recorded for the failed migration.
        assert (
            conn.execute("SELECT 1 FROM schema_migrations WHERE name='002_fails_once'").fetchone()
            is None
        )
    finally:
        conn.close()

    # Second run: the migration succeeds and is recorded.
    applied = database.run_migrations(tmp_path)
    assert applied == ["002_fails_once"]
    conn = database.connect(database.db_path(tmp_path))
    try:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='half_baked'"
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def test_migration_retry_loop_recovers_from_transient_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a migration hits 'database is locked' once, the retry loop refreshes
    the pending set and tries again, ultimately succeeding without re-running
    the already-applied migration."""

    database.run_migrations(tmp_path)

    state = {"raised": False}

    def _m_locks_once(conn: sqlite3.Connection) -> None:
        if not state["raised"]:
            state["raised"] = True
            raise sqlite3.OperationalError("database is locked")
        conn.execute("CREATE TABLE IF NOT EXISTS locked_then_ok (id TEXT)")

    monkeypatch.setattr(
        database,
        "MIGRATIONS",
        [*database.MIGRATIONS, ("002_locks_once", _m_locks_once)],
    )

    # Shorten the sleep so the retry doesn't drag the test out.
    monkeypatch.setattr(database.time, "sleep", lambda _seconds: None)

    applied = database.run_migrations(tmp_path)
    assert applied == ["002_locks_once"]


def test_reset_processing_to_queued_bumps_updated_at_and_preserves_prior_error(
    tmp_path: Path,
) -> None:
    """Documented Audicle-vs-MinusPod divergence: every UPDATE bumps updated_at
    explicitly (no triggers). And a pre-existing error is preserved so ops can
    tell a crash-restart from a clean restart."""

    database.run_migrations(tmp_path)
    conn = database.connect(database.db_path(tmp_path))
    try:
        conn.execute(
            "INSERT INTO jobs (id, url, episode_id, status, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("j-old", "https://x.test/a", "abc", "processing", "2020-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO jobs (id, url, episode_id, status, error, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "j-with-err",
                "https://x.test/b",
                "def",
                "processing",
                "llm_timeout after 300s",
                "2020-01-01T00:00:00Z",
            ),
        )

        reset = database.reset_processing_to_queued(conn)
        assert reset == 2

        old = conn.execute(
            "SELECT status, error, updated_at FROM jobs WHERE id = 'j-old'"
        ).fetchone()
        assert old["status"] == "queued"
        assert old["error"] == "reset on restart"
        assert old["updated_at"] > "2020-01-01T00:00:00Z"

        err = conn.execute(
            "SELECT status, error, updated_at FROM jobs WHERE id = 'j-with-err'"
        ).fetchone()
        assert err["status"] == "queued"
        assert err["error"] == "llm_timeout after 300s"
        assert err["updated_at"] > "2020-01-01T00:00:00Z"
    finally:
        conn.close()


def test_connect_enables_wal_and_foreign_keys(tmp_path: Path) -> None:
    conn = database.connect(database.db_path(tmp_path))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_prune_backups_removes_only_old(tmp_path: Path) -> None:
    old = tmp_path / f"{database.BACKUP_PREFIX}old.db"
    recent = tmp_path / f"{database.BACKUP_PREFIX}recent.db"
    old.write_text("x")
    recent.write_text("x")

    import os
    import time

    very_old = time.time() - 60 * 86400
    os.utime(old, (very_old, very_old))

    removed = database.prune_backups(tmp_path, retention_days=30)
    assert removed == [old]
    assert not old.exists()
    assert recent.exists()


def test_migration_lock_serializes_concurrent_callers(tmp_path: Path) -> None:
    """The fcntl lock is the only thing preventing two startups from racing on
    schema state. A second caller blocks until the first releases the lock."""

    holder_started = threading.Event()
    release_holder = threading.Event()
    second_acquired = threading.Event()

    def _holder() -> None:
        with database.migration_lock(tmp_path):
            holder_started.set()
            release_holder.wait(timeout=5)

    def _follower() -> None:
        holder_started.wait(timeout=5)
        with database.migration_lock(tmp_path):
            second_acquired.set()

    t1 = threading.Thread(target=_holder)
    t2 = threading.Thread(target=_follower)
    t1.start()
    t2.start()

    assert holder_started.wait(timeout=5)
    # While the holder still has the lock the follower must be blocked.
    assert not second_acquired.wait(timeout=0.5)
    release_holder.set()
    assert second_acquired.wait(timeout=5)
    t1.join(timeout=5)
    t2.join(timeout=5)
