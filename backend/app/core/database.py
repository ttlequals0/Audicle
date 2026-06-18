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

import asyncio
import fcntl
import logging
import os
import shutil
import sqlite3
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("app.core.database")

DB_FILENAME = "podcast.db"
LOCK_FILENAME = ".migration.lock"
REFERENCE_LOCK_FILENAME = ".reference.lock"
BACKUP_PREFIX = "podcast.db.backup-"


def db_path(data_dir: Path) -> Path:
    return Path(data_dir) / DB_FILENAME


def connect(
    path: Path, *, timeout: float = 30.0, check_same_thread: bool = True
) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and a sane synchronous setting.

    Falls back to DELETE journal if the WAL pragma fails (typically a filesystem
    that doesn't support shared-memory mapping), then retries WAL once the journal
    has been reset. Mirrors MinusPod's WAL-fallback logic.

    ``check_same_thread=False`` is for request-scoped connections (see
    ``deps.get_conn``): FastAPI may create the connection on one thread and use it
    on another within a single request. It is never shared concurrently, so the
    same-thread assertion is safe to relax there. Background/worker callers keep
    the default ``True``.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path), timeout=timeout, isolation_level=None, check_same_thread=check_same_thread
    )
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
def connection(
    data_dir: Path, *, timeout: float = 30.0, check_same_thread: bool = True
) -> Iterator[sqlite3.Connection]:
    """``with connection(settings.DATA_DIR) as conn:`` — replaces every
    open + try/finally close pair across services and routes.

    Identical semantics to ``connect(db_path(data_dir))`` plus guaranteed
    close on exception; written as a context manager so the 20-odd call
    sites collapse to one line.
    """

    conn = connect(db_path(data_dir), timeout=timeout, check_same_thread=check_same_thread)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _flock_exclusive(data_dir: Path, filename: str) -> Iterator[None]:
    """Hold an exclusive fcntl.flock on ``data_dir/filename`` for the block.

    The lock lives on the kernel file descriptor, so it serializes across
    processes (the ``uvicorn --workers N`` case) -- unlike an in-process
    asyncio.Lock, which each worker holds its own copy of.
    """

    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / filename
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def migration_lock(data_dir: Path) -> Iterator[None]:
    """Serialize concurrent startups via fcntl.flock on .migration.lock."""

    with _flock_exclusive(data_dir, LOCK_FILENAME):
        yield


@asynccontextmanager
async def reference_lock_async(data_dir: Path) -> AsyncIterator[None]:
    """Cross-process exclusive lock for the reference-voice critical section,
    safe to hold across awaits.

    A slot audition (``/api/v1/reference/slots/{n}/audition``) selects a slot on the
    wrapper, generates, then reloads the resting voice; an in-process asyncio.Lock can't
    serialize those across ``uvicorn --workers N``, so two auditions race and one can
    leave the wrapper switched to the wrong slot. This flock closes that gap.

    Uses a non-blocking flock with async retry rather than a blocking acquire on
    a worker thread: every suspension point is an ``asyncio.sleep`` holding only
    the fd (which the ``finally`` always closes), so a cancelled request -- e.g.
    the client disconnecting during a contended wait -- can never orphan the
    lock. A blocking ``flock`` run via ``asyncio.to_thread`` could be cancelled
    after the thread acquired the lock, leaving it held until process exit.
    """

    data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(data_dir / REFERENCE_LOCK_FILENAME, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
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


def _m007_episode_cleaned_text_size(conn: sqlite3.Connection) -> None:
    """Store the cleaned article text (the TTS input) and the audio file size.

    ``cleaned_text`` lets the API serve the cleaned article per episode (it was
    previously discarded after chunking). ``audio_size_bytes`` is recorded at
    finalize so the episodes list and RSS enclosure no longer stat() the file per
    request. Both are additive/NULL for episodes processed before 0.6.0; readers
    fall back (stat the file, or 404 the text) when NULL.
    """

    conn.execute("ALTER TABLE episodes ADD COLUMN cleaned_text TEXT")
    conn.execute("ALTER TABLE episodes ADD COLUMN audio_size_bytes INTEGER")


def _m008_backfill_cleaned_text_from_vtt(conn: sqlite3.Connection) -> None:
    """Backfill ``cleaned_text`` from the existing VTT for pre-0.6.0 episodes.

    Migration 007 added ``cleaned_text`` as NULL for existing rows, so
    ``/media/{id}.txt`` 404s for episodes processed before 0.6.0 even though
    their transcript still holds the narrated words. Reconstruct the text from
    the VTT. Additive UPDATE only -- rows without a usable VTT stay NULL -- and
    idempotent, since it only touches rows still NULL.
    """

    # Local import: keep core->services dependency at call time, not module load.
    from app.services import transcript

    rows = conn.execute(
        "SELECT id, transcript_vtt FROM episodes "
        "WHERE cleaned_text IS NULL AND transcript_vtt IS NOT NULL AND transcript_vtt != ''"
    ).fetchall()
    for row in rows:
        text = transcript.text_from_vtt(row["transcript_vtt"])
        if text:
            conn.execute(
                "UPDATE episodes SET cleaned_text = ? WHERE id = ?",
                (text, row["id"]),
            )


def _m009_job_started_at(conn: sqlite3.Connection) -> None:
    """Add the timestamp set when the worker claims a job (queued -> processing).

    Lets the UI show true synthesis time (started_at..updated_at) instead of
    queue-wait-inclusive elapsed. Additive/NULL for jobs created before 0.11.0
    and for any job still queued; readers skip the duration when NULL.
    """

    conn.execute("ALTER TABLE jobs ADD COLUMN started_at TEXT")


def _m010_episode_revision(conn: sqlite3.Connection) -> None:
    """Add a per-episode reprocess counter. Starts at 1 for every existing/new
    episode; the finalize upsert increments it on each in-place reprocess.

    Historically the feed GUID carried ``-r{revision}``, but that reset to 1 on a
    delete-then-resubmit and collided the GUID; the feed now versions the GUID by
    ``updated_at`` instead. The column is retained as an audit/debug counter.
    """

    conn.execute("ALTER TABLE episodes ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")


def _legacy_corrections_path() -> Path:
    """Where the pre-0.12.0 bind-mounted pronunciation dictionary lives, read once
    by the import migration. A function (not a constant) so tests can patch it."""

    return Path(__file__).parent.parent / "corrections" / "pronunciation.json"


def _m011_import_corrections_to_db(conn: sqlite3.Connection) -> None:
    """Import a legacy on-disk pronunciation dictionary into the settings table.

    Corrections moved from a bind-mounted ``pronunciation.json`` to a DB row in
    0.12.0. An operator who customized the dictionary has it on disk; import it
    once (only if no DB row exists yet and the file is non-empty) so the move
    preserves their entries. The empty default makes "non-empty == customized"
    unambiguous. Writes via raw ``conn.execute`` (no commit) so it stays inside
    the migration's atomic transaction.
    """

    import json

    # Local import: keep the core->services dependency at call time, not load.
    from app.services import corrections, settings_store

    existing = conn.execute(
        "SELECT 1 FROM settings WHERE key = ?", (settings_store.PRONUNCIATION_KEY,)
    ).fetchone()
    if existing is not None:
        return
    try:
        dictionary = corrections.load(_legacy_corrections_path())
    except (ValueError, OSError) as exc:
        logger.warning(
            "Legacy corrections file unreadable; skipping DB import",
            extra={"event": "corrections_import_skipped", "error": str(exc)},
        )
        return
    if not dictionary:
        return
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (settings_store.PRONUNCIATION_KEY, json.dumps(dictionary, ensure_ascii=False)),
    )


def _m012_lexicon_table(conn: sqlite3.Connection) -> None:
    """Create the ``lexicon`` table and load seed rows + migrate the user dict.

    All pronunciation data moves into one table: the curated seed CSV as read-only
    ``origin='seed'`` rows, and the legacy flat ``settings.pronunciation_dict`` as
    editable ``origin='user'`` rows. IPA is NOT derived here (gruut would make every
    test's migration slow); it is left NULL and populated by the offline base-lexicon
    build. The legacy settings row is preserved (never lose data). Runs via raw
    ``conn.execute`` (no commit) inside the migration's atomic transaction.
    """

    import json

    from app.services import lexicon, seed_corrections, settings_store

    lexicon.create_schema(conn)

    try:
        seed_entries = seed_corrections.load_seed(seed_corrections.seed_path())
    except Exception:
        logger.warning("Seed CSV unreadable during lexicon import", exc_info=True)
        seed_entries = []
    lexicon.import_readonly(conn, "seed", seed_corrections.build_lexicon_rows(seed_entries))

    existing = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (settings_store.PRONUNCIATION_KEY,)
    ).fetchone()
    if existing and existing["value"]:
        try:
            flat = json.loads(existing["value"])
        except (ValueError, TypeError):
            flat = {}
        user_rows = {
            k: seed_corrections.lexicon_row(k, v, None, "user-migrated")
            for k, v in (flat.items() if isinstance(flat, dict) else [])
            if isinstance(k, str) and isinstance(v, str) and k and v
        }
        if user_rows:
            lexicon.insert_entries(conn, "user", user_rows, read_only=False)


def _m013_episode_source_type(conn: sqlite3.Connection) -> None:
    """Add source provenance columns for the direct file-upload feature (0.30.0).

    ``source_type`` distinguishes a URL-sourced episode ('url', the default for
    every existing row) from an uploaded document ('upload'); the RSS feed and
    Feed UI branch on it so an upload's synthetic ``upload://`` original_url is
    never rendered as a broken hyperlink. ``source_filename`` is the original
    uploaded filename, shown in place of a source domain. Both additive: existing
    rows default to 'url' / NULL and keep rendering unchanged.
    """

    conn.execute("ALTER TABLE episodes ADD COLUMN source_type TEXT NOT NULL DEFAULT 'url'")
    conn.execute("ALTER TABLE episodes ADD COLUMN source_filename TEXT")


def _m014_job_columns(conn: sqlite3.Connection) -> None:
    """Add the reference-voice slot and reprocess flag to jobs (0.31.0).

    ``voice_id`` records which reference slot a job used (NULL = legacy voice.wav
    or a pre-0.31.0 row). ``reprocess`` persists what was a transient create_job
    param so webhooks and the Recents UI can tell a first run from a reprocess.
    Both additive; existing rows get NULL / 0.
    """

    conn.execute("ALTER TABLE jobs ADD COLUMN voice_id TEXT")
    conn.execute("ALTER TABLE jobs ADD COLUMN reprocess INTEGER NOT NULL DEFAULT 0")


def _m015_upload_max_mb(conn: sqlite3.Connection) -> None:
    """Convert a stored UPLOAD_MAX_BYTES override to UPLOAD_MAX_MB (0.31.0).

    The upload cap moved from bytes to megabytes. An operator who set the bytes
    value via the Settings UI has a ``runtime_settings`` row; convert it in place so
    the cap is preserved across the rename (the env-var fallback is handled in
    config). Idempotent: only writes the new key when the old exists and the new
    doesn't, then drops the old key. Runs via raw execute inside the migration txn.
    """

    import json

    old = conn.execute(
        "SELECT value FROM runtime_settings WHERE key = 'UPLOAD_MAX_BYTES'"
    ).fetchone()
    if old is None:
        return
    has_new = conn.execute(
        "SELECT 1 FROM runtime_settings WHERE key = 'UPLOAD_MAX_MB'"
    ).fetchone()
    if has_new is None:
        try:
            mb = max(1, int(json.loads(old["value"])) // (1024 * 1024))
        except (ValueError, TypeError):
            mb = None
        if mb is not None:
            conn.execute(
                "INSERT INTO runtime_settings (key, value) VALUES ('UPLOAD_MAX_MB', ?)",
                (json.dumps(mb),),
            )
    conn.execute("DELETE FROM runtime_settings WHERE key = 'UPLOAD_MAX_BYTES'")


def _m016_episode_voice_label(conn: sqlite3.Connection) -> None:
    """Add a human-readable ``voice_label`` to episodes and backfill it (0.31.x).

    New episodes get the label at finalize. For existing rows, derive it from the
    job's ``voice_id``: a NULL/blank id (or a job since purged) means the legacy
    ``voice.wav`` -- which is all any pre-slots episode could have used -- so it
    backfills to 'Default'; a recorded slot backfills to 'Slot N' (the slot's
    display label, if any, is restored on the next reprocess).
    """

    conn.execute("ALTER TABLE episodes ADD COLUMN voice_label TEXT")
    conn.execute(
        """
        UPDATE episodes SET voice_label = COALESCE((
            SELECT CASE
                WHEN j.voice_id IS NULL OR j.voice_id = '' THEN 'Default'
                ELSE 'Slot ' || j.voice_id
            END
            FROM jobs j WHERE j.id = episodes.job_id
        ), 'Default')
        WHERE voice_label IS NULL
        """
    )


def _reimport_seed_lexicon(conn: sqlite3.Connection) -> None:
    """Re-import the seed corrections from the shipped CSV, replacing only the read-only
    ``origin='seed'`` rows -- user corrections and base rows are untouched. The seed-resync
    migrations call this so an existing DB picks up a later trim of the seed list; a fresh
    DB that already loaded the current CSV in ``_m012`` gets an identical no-op re-import."""

    from app.services import lexicon, seed_corrections

    try:
        seed_entries = seed_corrections.load_seed(seed_corrections.seed_path())
    except Exception:
        logger.warning("Seed CSV unreadable during re-import", exc_info=True)
        return
    lexicon.import_readonly(conn, "seed", seed_corrections.build_lexicon_rows(seed_entries))


def _m017_reimport_seed_lexicon(conn: sqlite3.Connection) -> None:
    """0.34.0: re-sync the seed after the pseudo-phonetic respellings were removed."""

    _reimport_seed_lexicon(conn)


def _m019_reimport_seed_lexicon(conn: sqlite3.Connection) -> None:
    """0.36.0: re-sync the seed after the letter-spelled acronyms were removed -- Chatterbox
    pronounces common acronyms natively, so the spaced 'C E O' form was an XTTS-2-era crutch."""

    _reimport_seed_lexicon(conn)


def _m018_voice_wav_to_slot1(conn: sqlite3.Connection) -> None:
    """Migrate the legacy committed ``voice.wav`` into voice slot 1 (0.35.0).

    The slots-only model dropped the separate ``voice.wav`` as the default voice.
    On an existing install the operator's committed clip lives at
    ``reference/voice.wav``; copy it into the lowest empty slot it can take (slot 1)
    so that voice survives the cut-over. Copy, not move, so a rollback to pre-0.35.0
    still finds ``voice.wav``. A no-op when there is no committed clip (a fresh
    install -- the operator uploads slots directly) or slot 1 is already filled. This
    touches the bind-mounted reference dir, not ``conn``; the unused parameter keeps the
    migration signature uniform. Best-effort: a copy failure (e.g. a read-only mount) is
    logged and swallowed so it never blocks app boot. The write is atomic (temp +
    os.replace), so a crash mid-copy can't leave a truncated slot the ``slot1.is_file()``
    guard would then treat as filled.
    """

    from app.services import voices
    from app.services.atomic_write import write_bytes_atomic

    legacy = voices.voices_dir().parent / "voice.wav"
    slot1 = voices.slot_path(1)
    if not legacy.is_file() or slot1.is_file():
        return
    try:
        write_bytes_atomic(slot1, legacy.read_bytes(), prefix=".slot-migrate-")
    except OSError:
        logger.warning(
            "Could not migrate voice.wav into slot 1; upload a voice slot via the UI",
            extra={"event": "voice_wav_migrate_failed"},
            exc_info=True,
        )
        return
    logger.info(
        "Migrated legacy voice.wav into slot 1",
        extra={"event": "voice_wav_migrated_to_slot1"},
    )


MIGRATIONS: list[tuple[str, Migration]] = [
    ("001_initial_schema", _m001_initial_schema),
    ("002_settings_kv", _m002_settings_kv),
    ("003_auth_lockout", _m003_auth_lockout),
    ("004_runtime_settings", _m004_runtime_settings),
    ("005_episode_summary", _m005_episode_summary),
    ("006_job_progress", _m006_job_progress),
    ("007_episode_cleaned_text_size", _m007_episode_cleaned_text_size),
    ("008_backfill_cleaned_text_from_vtt", _m008_backfill_cleaned_text_from_vtt),
    ("009_job_started_at", _m009_job_started_at),
    ("010_episode_revision", _m010_episode_revision),
    ("011_import_corrections_to_db", _m011_import_corrections_to_db),
    ("012_lexicon_table", _m012_lexicon_table),
    ("013_episode_source_type", _m013_episode_source_type),
    ("014_job_columns", _m014_job_columns),
    ("015_upload_max_mb", _m015_upload_max_mb),
    ("016_episode_voice_label", _m016_episode_voice_label),
    ("017_reimport_seed_lexicon", _m017_reimport_seed_lexicon),
    ("018_voice_wav_to_slot1", _m018_voice_wav_to_slot1),
    ("019_reimport_seed_lexicon", _m019_reimport_seed_lexicon),
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
