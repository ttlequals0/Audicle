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
        "005_episode_summary",
        "006_job_progress",
        "007_episode_cleaned_text_size",
        "008_backfill_cleaned_text_from_vtt",
        "009_job_started_at",
        "010_episode_revision",
        "011_import_corrections_to_db",
        "012_lexicon_table",
        "013_episode_source_type",
        "014_job_columns",
        "015_upload_max_mb",
        "016_episode_voice_label",
        "017_reimport_seed_lexicon",
        "018_voice_wav_to_slot1",
        "019_reimport_seed_lexicon",
        "020_reimport_seed_lexicon",
        "021_drop_lexicon_ipa_column",
        "022_reimport_seed_lexicon",
        "023_reimport_seed_lexicon",
        "024_reimport_seed_lexicon",
        "025_episode_chapters_json",
        "026_lexicon_input_text_index",
        "027_episode_generations",
        "028_lexicon_generations",
        "029_feed_revision",
        "030_episode_audio_generation",
        "031_books",
        "032_job_recovery_count",
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
        "005_episode_summary",
        "006_job_progress",
        "007_episode_cleaned_text_size",
        "008_backfill_cleaned_text_from_vtt",
        "009_job_started_at",
        "010_episode_revision",
        "011_import_corrections_to_db",
        "012_lexicon_table",
        "013_episode_source_type",
        "014_job_columns",
        "015_upload_max_mb",
        "016_episode_voice_label",
        "017_reimport_seed_lexicon",
        "018_voice_wav_to_slot1",
        "019_reimport_seed_lexicon",
        "020_reimport_seed_lexicon",
        "021_drop_lexicon_ipa_column",
        "022_reimport_seed_lexicon",
        "023_reimport_seed_lexicon",
        "024_reimport_seed_lexicon",
        "025_episode_chapters_json",
        "026_lexicon_input_text_index",
        "027_episode_generations",
        "028_lexicon_generations",
        "029_feed_revision",
        "030_episode_audio_generation",
        "031_books",
        "032_job_recovery_count",
    ]
    assert second == []


def test_no_backup_on_fresh_init_or_noop(tmp_path: Path) -> None:
    database.run_migrations(tmp_path)
    database.run_migrations(tmp_path)
    assert sorted(tmp_path.glob(f"{database.BACKUP_PREFIX}*")) == []


def test_m016_backfills_voice_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Apply through 015, seed episodes + jobs, then let 016 backfill: a recorded
    # slot -> "Slot N", a NULL voice_id or a missing job -> "Default". Cut the list at
    # 016 by NAME rather than by a trailing count, so appending a migration doesn't
    # silently shift the slice and let 016 run before the rows exist.
    full = database.MIGRATIONS
    cutoff = next(i for i, (name, _) in enumerate(full) if name == "016_episode_voice_label")
    monkeypatch.setattr(database, "MIGRATIONS", full[:cutoff])
    database.run_migrations(tmp_path)
    conn = database.connect(database.db_path(tmp_path))
    try:
        conn.executescript(
            """
            INSERT INTO jobs (id, url, episode_id, status, voice_id)
              VALUES ('j1','https://x/1','e1','done','3'),
                     ('j2','https://x/2','e2','done',NULL);
            INSERT INTO episodes (id, job_id, original_url)
              VALUES ('e1','j1','https://x/1'),
                     ('e2','j2','https://x/2'),
                     ('e3',NULL,'https://x/3');
            """
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(database, "MIGRATIONS", full)
    database.run_migrations(tmp_path)
    conn = database.connect(database.db_path(tmp_path))
    try:
        got = {r["id"]: r["voice_label"] for r in conn.execute("SELECT id, voice_label FROM episodes")}
    finally:
        conn.close()
    assert got == {"e1": "Slot 3", "e2": "Default", "e3": "Default"}


def test_m018_copies_voice_wav_into_slot1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pre-0.35.0 install has a committed reference/voice.wav and an empty slot 1;
    # 018 copies the clip into slot 1 and leaves voice.wav in place (rollback safety).
    from app.services import voices

    vdir = tmp_path / "reference" / "voices"
    vdir.mkdir(parents=True)
    monkeypatch.setattr(voices, "voices_dir", lambda: vdir)
    (vdir.parent / "voice.wav").write_bytes(b"LEGACYVOICE")

    database.run_migrations(tmp_path)

    assert (vdir / "slot1.wav").read_bytes() == b"LEGACYVOICE"
    assert (vdir.parent / "voice.wav").is_file()  # copied, not moved


def test_m018_noop_when_slot1_already_filled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An operator who already uploaded slot 1 keeps it; the legacy clip doesn't clobber it.
    from app.services import voices

    vdir = tmp_path / "reference" / "voices"
    vdir.mkdir(parents=True)
    monkeypatch.setattr(voices, "voices_dir", lambda: vdir)
    (vdir.parent / "voice.wav").write_bytes(b"LEGACYVOICE")
    (vdir / "slot1.wav").write_bytes(b"EXISTINGSLOT")

    database.run_migrations(tmp_path)

    assert (vdir / "slot1.wav").read_bytes() == b"EXISTINGSLOT"


def test_m011_imports_legacy_corrections_into_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-empty legacy on-disk dictionary is imported into the settings table.
    legacy = tmp_path / "pronunciation.json"
    legacy.write_text('{"kubectl": "kube control"}', encoding="utf-8")
    monkeypatch.setattr(database, "_legacy_corrections_path", lambda: legacy)

    database.run_migrations(tmp_path)

    from app.services import corrections, settings_store

    conn = database.connect(database.db_path(tmp_path))
    try:
        assert settings_store.get(conn, settings_store.PRONUNCIATION_KEY) is not None
        assert corrections.load_user_dict(conn) == {"kubectl": "kube control"}
    finally:
        conn.close()


def test_m011_no_import_when_legacy_file_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "pronunciation.json"
    legacy.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(database, "_legacy_corrections_path", lambda: legacy)

    database.run_migrations(tmp_path)

    from app.services import settings_store

    conn = database.connect(database.db_path(tmp_path))
    try:
        assert settings_store.get(conn, settings_store.PRONUNCIATION_KEY) is None
    finally:
        conn.close()


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


def test_backup_includes_committed_wal_frames_held_by_reader(tmp_path: Path) -> None:
    database.run_migrations(tmp_path)
    path = database.db_path(tmp_path)
    path.chmod(0o600)
    reader = database.connect(path)
    writer = database.connect(path)
    try:
        writer.execute(
            "INSERT INTO jobs (id, url, episode_id, status) VALUES (?, ?, ?, ?)",
            ("snapshot-job", "https://x.test/snapshot", "snapshot", "queued"),
        )
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM jobs").fetchall()
        writer.execute(
            "INSERT INTO jobs (id, url, episode_id, status) VALUES (?, ?, ?, ?)",
            ("wal-job", "https://x.test/wal", "wal", "queued"),
        )
        checkpoint = writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        assert checkpoint[1] > checkpoint[2]
        backup = database._backup_db(writer, path)
        assert backup is not None
        assert backup.stat().st_mode & 0o777 == 0o600
    finally:
        reader.close()
        writer.close()

    restored = sqlite3.connect(backup)
    try:
        row = restored.execute("SELECT id FROM jobs WHERE id = 'wal-job'").fetchone()
        assert row == ("wal-job",)
    finally:
        restored.close()


def test_backup_failure_prevents_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database.run_migrations(tmp_path)
    conn = database.connect(database.db_path(tmp_path))
    try:
        conn.execute(
            "INSERT INTO jobs (id, url, episode_id, status) VALUES (?, ?, ?, ?)",
            ("j1", "https://x.test/a", "abc", "queued"),
        )
    finally:
        conn.close()

    def _migration(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE should_not_exist (id TEXT)")

    def _failed_backup(conn: sqlite3.Connection, path: Path) -> None:
        raise OSError("backup target unavailable")

    monkeypatch.setattr(database, "_backup_db", _failed_backup)
    monkeypatch.setattr(database, "MIGRATIONS", [*database.MIGRATIONS, ("027_test", _migration)])
    with pytest.raises(OSError, match="backup target unavailable"):
        database.run_migrations(tmp_path)

    conn = database.connect(database.db_path(tmp_path))
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'should_not_exist'"
        ).fetchone() is None
    finally:
        conn.close()


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


def test_m008_backfills_cleaned_text_from_vtt(tmp_path: Path) -> None:
    """Pre-0.6.0 episodes (cleaned_text NULL) get text reconstructed from their
    VTT; episodes without a VTT stay NULL."""

    database.run_migrations(tmp_path)
    conn = database.connect(database.db_path(tmp_path))
    try:
        vtt = (
            "WEBVTT\n\n"
            "1\n00:00:00.000 --> 00:00:02.000\nHello world.\n\n"
            "2\n00:00:02.000 --> 00:00:04.000\nThe A.P.I. is fast &amp; clean.\n"
        )
        conn.execute(
            "INSERT INTO episodes (id, original_url, transcript_vtt, cleaned_text) "
            "VALUES (?, ?, ?, NULL)",
            ("ep-old", "https://x.test/old", vtt),
        )
        conn.execute(
            "INSERT INTO episodes (id, original_url, transcript_vtt, cleaned_text) "
            "VALUES (?, ?, NULL, NULL)",
            ("ep-novtt", "https://x.test/novtt"),
        )

        database._m008_backfill_cleaned_text_from_vtt(conn)

        old = conn.execute("SELECT cleaned_text FROM episodes WHERE id='ep-old'").fetchone()
        assert old["cleaned_text"] == "Hello world.\n\nThe A.P.I. is fast & clean."
        novtt = conn.execute("SELECT cleaned_text FROM episodes WHERE id='ep-novtt'").fetchone()
        assert novtt["cleaned_text"] is None
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


async def test_reference_lock_async_excludes_other_holders(tmp_path: Path) -> None:
    """While reference_lock_async is held, another fd (standing in for a second
    worker process) must not be able to acquire the same flock -- this is what
    serializes the reference-voice critical section across uvicorn --workers N."""

    import fcntl
    import os

    async with database.reference_lock_async(tmp_path):
        fd = os.open(
            tmp_path / database.REFERENCE_LOCK_FILENAME, os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    # Released on exit: a fresh acquire now succeeds.
    fd = os.open(
        tmp_path / database.REFERENCE_LOCK_FILENAME, os.O_CREAT | os.O_RDWR, 0o600
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


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


def test_m015_converts_upload_bytes_override_to_mb(tmp_path: Path) -> None:
    import json

    database.run_migrations(tmp_path)
    conn = database.connect(database.db_path(tmp_path))
    try:
        conn.execute(
            "INSERT INTO runtime_settings (key, value) VALUES ('UPLOAD_MAX_BYTES', ?)",
            (json.dumps(100 * 1024 * 1024),),
        )
        conn.commit()
        database._m015_upload_max_mb(conn)
        conn.commit()
        rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM runtime_settings")}
        assert "UPLOAD_MAX_BYTES" not in rows
        assert json.loads(rows["UPLOAD_MAX_MB"]) == 100
    finally:
        conn.close()


def test_lexicon_input_text_index_exists(env: Path) -> None:
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        names = {
            row["name"]
            for row in conn.execute("PRAGMA index_list('lexicon')")
        }
        assert "idx_lexicon_input_text" in names
        # The exact-case probe must be index-served, not a table scan.
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM lexicon WHERE case_sensitive = 1 AND input_text = 'x'"
        ).fetchall()
        assert any("idx_lexicon_input_text" in row["detail"] for row in plan)
    finally:
        conn.close()


def test_m031_is_additive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    full = database.MIGRATIONS
    cutoff = next(i for i, (name, _) in enumerate(full) if name == "031_books")
    monkeypatch.setattr(database, "MIGRATIONS", full[:cutoff])
    database.run_migrations(tmp_path)
    conn = database.connect(database.db_path(tmp_path))
    try:
        conn.executescript(
            """
            INSERT INTO jobs (id, url, episode_id, status, voice_id)
              VALUES ('j1','https://x/1','e1','done','3');
            INSERT INTO episodes (id, job_id, original_url, title)
              VALUES ('e1','j1','https://x/1','T');
            """
        )
        before_job = tuple(conn.execute("SELECT * FROM jobs").fetchone())
        before_ep = dict(conn.execute("SELECT * FROM episodes").fetchone())
    finally:
        conn.close()
    monkeypatch.setattr(database, "MIGRATIONS", full)
    assert database.run_migrations(tmp_path) == ["031_books", "032_job_recovery_count"]
    conn = database.connect(database.db_path(tmp_path))
    try:
        assert tuple(conn.execute("SELECT * FROM jobs").fetchone())[: len(before_job)] == before_job
        after_ep = dict(conn.execute("SELECT * FROM episodes").fetchone())
        assert after_ep.pop("season_number") is None
        assert after_ep.pop("episode_number") is None
        assert after_ep == before_ep
        index = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_jobs_episode_id'"
        ).fetchone()
        assert index is not None
        database._m031_books(conn)  # idempotent re-run
    finally:
        conn.close()


def _jobs_conn(tmp_path: Path):
    database.run_migrations(tmp_path)
    return database.connect(database.db_path(tmp_path))


def _insert_processing(conn, job_id: str, count: int = 0, error: str | None = None) -> None:
    conn.execute(
        "INSERT INTO jobs (id, url, episode_id, status, error, recovery_count) "
        "VALUES (?, ?, ?, 'processing', ?, ?)",
        (job_id, f"https://x.test/{job_id}", job_id, error, count),
    )


def _job(conn, job_id: str):
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def test_migration_032_adds_recovery_count_and_keeps_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    full = database.MIGRATIONS
    cutoff = next(i for i, (name, _) in enumerate(full) if name == "032_job_recovery_count")
    monkeypatch.setattr(database, "MIGRATIONS", full[:cutoff])
    database.run_migrations(tmp_path)
    conn = database.connect(database.db_path(tmp_path))
    try:
        conn.execute(
            "INSERT INTO jobs (id, url, episode_id, status, error, voice_id) "
            "VALUES ('j1','https://x/1','e1','failed','boom','3')"
        )
        before = dict(conn.execute("SELECT * FROM jobs").fetchone())
    finally:
        conn.close()
    monkeypatch.setattr(database, "MIGRATIONS", full)
    assert database.run_migrations(tmp_path) == ["032_job_recovery_count"]
    conn = database.connect(database.db_path(tmp_path))
    try:
        after = dict(conn.execute("SELECT * FROM jobs").fetchone())
        assert after.pop("recovery_count") == 0
        assert after == before
    finally:
        conn.close()


def test_reset_processing_requeues_and_counts(tmp_path: Path) -> None:
    conn = _jobs_conn(tmp_path)
    try:
        _insert_processing(conn, "a")
        _insert_processing(conn, "b", error="llm_timeout")
        assert database.reset_processing_to_queued(conn) == 2
        a, b = _job(conn, "a"), _job(conn, "b")
        assert (a["status"], a["recovery_count"], a["error"]) == ("queued", 1, "reset on restart")
        assert (b["status"], b["recovery_count"], b["error"]) == ("queued", 1, "llm_timeout")
    finally:
        conn.close()


def test_reset_processing_fails_job_after_max_recoveries(tmp_path: Path) -> None:
    conn = _jobs_conn(tmp_path)
    try:
        _insert_processing(conn, "dead", count=database.MAX_CRASH_RECOVERIES)
        _insert_processing(conn, "live", count=database.MAX_CRASH_RECOVERIES - 1)
        assert database.reset_processing_to_queued(conn) == 1
        dead, live = _job(conn, "dead"), _job(conn, "live")
        assert dead["status"] == "failed"
        assert dead["error"] == (
            f"worker stopped {database.MAX_CRASH_RECOVERIES} times while processing "
            "this job; not retried"
        )
        assert dead["recovery_count"] == database.MAX_CRASH_RECOVERIES
        assert live["status"] == "queued"
    finally:
        conn.close()


def test_job_survives_exactly_max_recoveries(tmp_path: Path) -> None:
    conn = _jobs_conn(tmp_path)
    try:
        _insert_processing(conn, "j")
        for _ in range(database.MAX_CRASH_RECOVERIES):
            assert database.reset_processing_to_queued(conn) == 1
            assert _job(conn, "j")["status"] == "queued"
            conn.execute("UPDATE jobs SET status = 'processing' WHERE id = 'j'")
        assert database.reset_processing_to_queued(conn) == 0
        assert _job(conn, "j")["status"] == "failed"
    finally:
        conn.close()


def test_operator_requeue_resets_recovery_count(tmp_path: Path) -> None:
    from app.services import jobs

    conn = _jobs_conn(tmp_path)
    try:
        url = "https://x.test/article"
        first = jobs.create_job(conn, url).job
        conn.execute(
            "UPDATE jobs SET status = 'processing', recovery_count = ? WHERE id = ?",
            (database.MAX_CRASH_RECOVERIES, first.id),
        )
        database.reset_processing_to_queued(conn)
        assert _job(conn, first.id)["status"] == "failed"
        again = jobs.create_job(conn, url, reprocess=True).job
        row = _job(conn, again.id)
        assert (row["status"], row["recovery_count"]) == ("queued", 0)
    finally:
        conn.close()
