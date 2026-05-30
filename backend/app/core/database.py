"""SQLite connection helpers and idempotent migration runner.

Pattern lifted from MinusPod's database/schema/__init__.py with these Audicle
fixes:
- Migrations run inside explicit BEGIN IMMEDIATE / COMMIT so the migration body
  and the schema_migrations INSERT are atomic even though the connection is in
  autocommit mode for everything else (sqlite3's ``with conn:`` is a no-op
  under ``isolation_level=None``).
- WAL is checkpointed (TRUNCATE) before the backup copy so the .db file is
  self-contained; the -wal / -shm sidecars never need to ride along.
- Application-managed updated_at: every UPDATE statement sets it explicitly
  (no triggers). MinusPod's schema leaves updated_at frozen on writes; Audicle
  does not.
- A pending-aware retry loop: only un-applied migrations are re-run on a lock
  retry, so a transient lock can't trigger a duplicate INSERT.
"""

from __future__ import annotations

import fcntl
import logging
import os
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("app.core.database")

DB_FILENAME = "podcast.db"
LOCK_FILENAME = ".migration.lock"
BACKUP_PREFIX = "podcast.db.backup-"


def db_path(data_dir: Path) -> Path:
    return Path(data_dir) / DB_FILENAME


def connect(path: Path, *, timeout: float = 30.0) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and a sane synchronous setting.

    Falls back to DELETE journal if the WAL pragma fails (typically a filesystem
    that doesn't support shared-memory mapping), then retries WAL once the journal
    has been reset. Mirrors MinusPod's WAL-fallback logic.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        logger.warning("WAL pragma failed; falling back to DELETE journal and retrying")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def connection(data_dir: Path, *, timeout: float = 30.0) -> Iterator[sqlite3.Connection]:
    """``with connection(settings.DATA_DIR) as conn:`` — replaces every
    open + try/finally close pair across services and routes.

    Identical semantics to ``connect(db_path(data_dir))`` plus guaranteed
    close on exception; written as a context manager so the 20-odd call
    sites collapse to one line.
    """

    conn = connect(db_path(data_dir), timeout=timeout)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def migration_lock(data_dir: Path) -> Iterator[None]:
    """Serialize concurrent startups via fcntl.flock on .migration.lock."""

    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / LOCK_FILENAME
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _backup_db(conn: sqlite3.Connection, path: Path) -> Path | None:
    """Copy the DB file to a timestamped backup before applying migrations.

    Checkpoints the WAL via ``PRAGMA wal_checkpoint(TRUNCATE)`` first so the
    main file is self-contained at the moment we copy it; the -wal and -shm
    sidecars don't need to be included in the backup.

    Returns the backup path, or None if the source DB doesn't exist yet.
    """

    if not path.exists():
        return None
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError as exc:
        logger.warning(
            "WAL checkpoint before backup failed; copy may miss un-checkpointed pages",
            extra={"event": "backup_checkpoint_failed", "error": str(exc)},
        )
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = path.with_name(f"{BACKUP_PREFIX}{stamp}")
    shutil.copy2(path, dest)
    logger.info(
        "DB backup written before migrations",
        extra={"event": "db_backup", "path": str(dest)},
    )
    return dest


# Migrations are idempotent: each one checks current schema state before applying.
# Order matters; new migrations append to the list and never mutate prior ones.

Migration = Callable[[sqlite3.Connection], None]


def _m001_initial_schema(conn: sqlite3.Connection) -> None:
    """Schema: jobs and episodes."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id           TEXT PRIMARY KEY,
            url          TEXT NOT NULL,
            episode_id   TEXT NOT NULL,
            status       TEXT NOT NULL,
            stage        TEXT,
            error        TEXT,
            created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS episodes (
            id              TEXT PRIMARY KEY,
            job_id          TEXT REFERENCES jobs(id),
            title           TEXT,
            author          TEXT,
            original_url    TEXT NOT NULL UNIQUE,
            audio_path      TEXT,
            artwork_path    TEXT,
            transcript_vtt  TEXT,
            duration_secs   INTEGER,
            pub_date        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_pub_date ON episodes(pub_date DESC)")


def _m002_settings_kv(conn: sqlite3.Connection) -> None:
    """Settings key/value store (podcast:guid, future runtime knobs)."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
        """
    )


def _m003_auth_lockout(conn: sqlite3.Connection) -> None:
    """Track failed login attempts + lockout window per identifier.

    ``identifier`` is the lower-cased username (single-user admin today but
    the schema doesn't bake that in). ``lockout_until`` is the ISO timestamp
    after which the next login attempt is allowed; NULL means no current
    lockout.
    """

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_lockout (
            identifier        TEXT PRIMARY KEY,
            failed_attempts   INTEGER NOT NULL DEFAULT 0,
            last_attempt_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            lockout_until     TEXT
        )
        """
    )


def _m004_runtime_settings(conn: sqlite3.Connection) -> None:
    """Operator-tunable settings that override env defaults at
    request time. Keys are constrained to the Phase-10 allowlist; values
    are stored as strings and coerced by the resolver."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
        """
    )


def _m005_episode_summary(conn: sqlite3.Connection) -> None:
    """Add the episode show-notes summary column.

    Additive ALTER (NULL for existing rows); the feed falls back to the
    title/author/source description when ``summary`` is NULL, so old episodes
    keep rendering unchanged.
    """

    conn.execute("ALTER TABLE episodes ADD COLUMN summary TEXT")


def _m006_job_progress(conn: sqlite3.Connection) -> None:
    """Add per-stage progress counters so the UI can show "chunk X of Y".

    Additive (NULL for existing/older jobs and for stages that don't report
    progress). Populated by the tts/cleanup stages; reset on each stage change.
    """

    conn.execute("ALTER TABLE jobs ADD COLUMN progress_current INTEGER")
    conn.execute("ALTER TABLE jobs ADD COLUMN progress_total INTEGER")


MIGRATIONS: list[tuple[str, Migration]] = [
    ("001_initial_schema", _m001_initial_schema),
    ("002_settings_kv", _m002_settings_kv),
    ("003_auth_lockout", _m003_auth_lockout),
    ("004_runtime_settings", _m004_runtime_settings),
    ("005_episode_summary", _m005_episode_summary),
    ("006_job_progress", _m006_job_progress),
]


def run_migrations(data_dir: Path) -> list[str]:
    """Apply pending migrations under an exclusive lock. Returns names of migrations run.

    The schema_migrations table records which migrations have been applied so a
    no-op startup neither writes a backup nor calls any migration body.
    """

    path = db_path(data_dir)
    applied: list[str] = []

    with migration_lock(data_dir):
        conn = connect(path)
        try:
            _ensure_meta_table(conn)
            if not _pending_names(conn):
                return applied

            if _db_has_user_tables(conn):
                _backup_db(conn, path)

            for attempt in range(5):
                pending = [(name, fn) for name, fn in MIGRATIONS if name in _pending_names(conn)]
                if not pending:
                    break
                try:
                    _apply_pending(conn, pending, applied)
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 4:
                        raise
                    sleep_for = 0.5 * (2**attempt)
                    logger.warning(
                        "Migration retry after lock contention",
                        extra={
                            "event": "migration_retry",
                            "attempt": attempt,
                            "sleep": sleep_for,
                        },
                    )
                    time.sleep(sleep_for)
        finally:
            conn.close()

    return applied


def _ensure_meta_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name        TEXT PRIMARY KEY,
            applied_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
        """
    )


def _pending_names(conn: sqlite3.Connection) -> set[str]:
    done = {row["name"] for row in conn.execute("SELECT name FROM schema_migrations")}
    return {name for name, _ in MIGRATIONS} - done


def _db_has_user_tables(conn: sqlite3.Connection) -> bool:
    """True if any application table exists (schema_migrations + sqlite-internal
    bookkeeping like sqlite_sequence don't count)."""

    return any(
        row[0] != "schema_migrations" and not row[0].startswith("sqlite_")
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    )


def _apply_pending(
    conn: sqlite3.Connection,
    pending: list[tuple[str, Migration]],
    applied: list[str],
) -> None:
    """Apply each migration body and its schema_migrations INSERT atomically.

    Uses explicit BEGIN IMMEDIATE / COMMIT because ``with conn:`` is a no-op
    when ``isolation_level=None`` (autocommit). Rollback on any exception so a
    partially-applied migration cannot leave the schema diverged from the
    schema_migrations record.
    """

    for name, migration in pending:
        # BEGIN is outside the rollback block: a failure here means no txn was
        # opened, so an explicit ROLLBACK would itself raise "no transaction is
        # active" and mask the real (often retryable) error.
        conn.execute("BEGIN IMMEDIATE")
        try:
            migration(conn)
            conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (name,))
            conn.execute("COMMIT")
        except Exception:
            # COMMIT failures auto-abort the txn in SQLite; in every other case
            # we want to undo the partial work. Either way, suppress the
            # secondary error so the original exception propagates.
            with suppress(sqlite3.OperationalError):
                conn.execute("ROLLBACK")
            raise
        applied.append(name)
        logger.info(
            "Migration applied",
            extra={"event": "migration_applied", "migration": name},
        )


def reset_processing_to_queued(conn: sqlite3.Connection) -> int:
    """Crash recovery: jobs that died mid-pipeline get picked up again.

    Build plan dictates an unconditional reset (single in-flight job, no race).
    Returns the row count affected.

    Preserves any prior `error` text by only stamping "reset on restart" when
    the column is NULL. A job that hit a real failure right before the worker
    crashed (e.g. "llm_timeout after 300s") keeps that diagnostic so ops can
    distinguish a clean restart from a recurring upstream problem.
    """

    cursor = conn.execute(
        """
        UPDATE jobs
        SET status = 'queued',
            error = COALESCE(error, 'reset on restart'),
            updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        WHERE status = 'processing'
        """
    )
    return cursor.rowcount


def prune_backups(data_dir: Path, retention_days: int) -> list[Path]:
    """Delete DB backups older than retention_days. Called by the daily retention sweep."""

    cutoff = time.time() - retention_days * 86400
    removed: list[Path] = []
    for entry in data_dir.glob(f"{BACKUP_PREFIX}*"):
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed.append(entry)
        except OSError as exc:
            logger.warning(
                "Failed to prune backup",
                extra={"event": "backup_prune_failed", "path": str(entry), "error": str(exc)},
            )
    return removed
