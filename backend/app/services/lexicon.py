"""The ``lexicon`` table: all pronunciation data in one place.

Three origins share one table:

- ``seed`` -- the curated in-repo CSV (read-only).
- ``base`` -- the full converted union of external sources (read-only, ~300k).
- ``user`` -- operator edits (the only mutable rows).

Read-only rows are loaded by a versioned import (migration / startup sync) and
refreshed when the bundled data version changes; user rows are never touched by
the import. Lookup precedence is user > seed > base, and a case-sensitive entry
only matches the exact casing while a case-insensitive one matches any casing.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import sqlite3
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("app.services.lexicon")

ORIGINS = ("user", "seed", "base")
_ORIGIN_RANK = {"user": 0, "seed": 1, "base": 2}

# Versioned read-only import: when the bundled artifact's version differs from the
# value stored here, the seed/base rows are refreshed (user rows untouched).
LEXICON_VERSION_KEY = "lexicon_version"


def default_artifact_path() -> Path:
    """Bundled base-lexicon artifact (built offline by scripts/build_base_lexicon.py).

    JSONL, optionally gzipped; absent in a normal checkout until the build runs.
    """

    data_dir = Path(__file__).resolve().parent.parent / "data"
    gz = data_dir / "base_lexicon.jsonl.gz"
    return gz if gz.exists() else data_dir / "base_lexicon.jsonl"


def sync_base_artifact(conn: sqlite3.Connection, artifact_path: Path, version: str) -> bool:
    """Import the base-lexicon artifact into read-only rows if the version changed.

    Idempotent and safe to call on every startup: a no-op when the stored version
    already matches or the artifact is absent. User rows are never touched. Returns
    True when an import ran.
    """

    from app.services import settings_store  # local import avoids a cycle

    if settings_store.get(conn, LEXICON_VERSION_KEY) == version:
        return False
    if not artifact_path.exists():
        return False
    # Import-friendly pragmas for the bulk insert (#126): the 1.3M-row base
    # layer against SQLite's 2 MB default page cache wrote ~14 GB for a 241 MB
    # artifact (page churn), and wal_autocheckpoint flushed dirty pages mid-
    # transaction so they were written twice. Both are per-connection settings;
    # restore them after so a long-lived caller keeps its defaults.
    prev_cache = conn.execute("PRAGMA cache_size").fetchone()[0]
    prev_ckpt = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    try:
        return _sync_base_artifact_locked(conn, artifact_path, version)
    finally:
        conn.execute(f"PRAGMA cache_size={int(prev_cache)}")
        conn.execute(f"PRAGMA wal_autocheckpoint={int(prev_ckpt)}")
        # One deliberate checkpoint so the WAL growth from the import lands in
        # the main file now instead of on the next unlucky writer.
        with suppress(sqlite3.OperationalError):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _sync_base_artifact_locked(
    conn: sqlite3.Connection, artifact_path: Path, version: str
) -> bool:
    from app.services import settings_store  # local import avoids a cycle

    by_origin: dict[str, dict[str, dict]] = {"seed": {}, "base": {}}
    opener = gzip.open if artifact_path.suffix == ".gz" else open
    with opener(artifact_path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            origin = obj.get("origin", "base")
            if origin not in by_origin or not obj.get("input_text") or not obj.get("spoken"):
                continue
            by_origin[origin][obj["input_text"]] = obj
    logger.info(
        "Base lexicon import starting",
        extra={
            "event": "lexicon_sync_started",
            "version": version,
            "seed": len(by_origin["seed"]),
            "base": len(by_origin["base"]),
            "batch_rows": _IMPORT_BATCH_ROWS,
        },
    )
    started = time.monotonic()
    for origin, entries in by_origin.items():
        if entries:
            import_readonly(conn, origin, entries)
    settings_store.set_(conn, LEXICON_VERSION_KEY, version)  # commits
    logger.info(
        "Base lexicon imported",
        extra={
            "event": "lexicon_import",
            "version": version,
            "seed": len(by_origin["seed"]),
            "base": len(by_origin["base"]),
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return True

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS lexicon (
    origin TEXT NOT NULL,
    input_text TEXT NOT NULL,
    input_fold TEXT NOT NULL,
    mode TEXT NOT NULL,
    spoken TEXT NOT NULL,
    case_sensitive INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 1.0,
    source TEXT,
    notes TEXT,
    read_only INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (origin, input_text)
);
"""
_CREATE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_lexicon_fold ON lexicon(input_fold);"
# The PK (origin, input_text) cannot serve a bare input_text probe (origin
# leads), so exact-case lookups need their own index; without it every
# ``lookup`` is a full scan of the 1.3M-row base lexicon.
_CREATE_TEXT_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_lexicon_input_text ON lexicon(input_text);"


@dataclass(frozen=True)
class LexEntry:
    origin: str
    input_text: str
    mode: str
    spoken: str
    case_sensitive: bool
    confidence: float
    source: str | None
    notes: str | None
    read_only: bool


def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(CREATE_TABLE_SQL)
    conn.execute(_CREATE_INDEX_SQL)
    conn.execute(_CREATE_TEXT_INDEX_SQL)


def _row_to_entry(row: sqlite3.Row) -> LexEntry:
    return LexEntry(
        origin=row["origin"],
        input_text=row["input_text"],
        mode=row["mode"],
        spoken=row["spoken"],
        case_sensitive=bool(row["case_sensitive"]),
        confidence=row["confidence"],
        source=row["source"],
        notes=row["notes"],
        read_only=bool(row["read_only"]),
    )


def lookup(conn: sqlite3.Connection, token: str) -> LexEntry | None:
    """Best lexicon entry for ``token``, or None.

    A case-sensitive row must match the exact casing; a case-insensitive row
    matches any casing. Among candidates, an exact-case match beats a folded
    match, then origin precedence user > seed > base wins.
    """

    fold = token.casefold()
    # Two indexed probes instead of an OR: the OR form forced a full-table
    # scan (input_text side had no usable index), which at one lookup per
    # token made the corrections backstop the slowest part of normalize.
    # The branches are disjoint (case_sensitive differs), so UNION ALL is
    # exactly the old predicate.
    rows = conn.execute(
        """
        SELECT * FROM lexicon WHERE case_sensitive = 1 AND input_text = ?
        UNION ALL
        SELECT * FROM lexicon WHERE case_sensitive = 0 AND input_fold = ?
        """,
        (token, fold),
    ).fetchall()
    if not rows:
        return None
    candidates = [_row_to_entry(r) for r in rows]
    candidates.sort(
        key=lambda e: (
            0 if (e.case_sensitive and e.input_text == token) else 1,
            _ORIGIN_RANK.get(e.origin, 9),
        )
    )
    return candidates[0]


def apply_pairs_by_case(conn: sqlite3.Connection) -> tuple[dict[str, str], dict[str, str]]:
    """``(case_sensitive, case_insensitive)`` ``{input_text: spoken}`` maps for user +
    seed rows, split by each row's ``case_sensitive`` flag, user winning on collision.

    Feeds the longest-key-first regex substitution (multi-word phrases). The large
    base layer is applied per-token via :func:`lookup`, not here. Splitting by case
    lets ``corrections.apply`` fold the case-insensitive group so an entry keyed
    ``404 media`` hits ``404 Media``, while ``US``/``OS`` stay exact-case.
    """

    rows: dict[str, tuple[str, bool]] = {}
    for origin in ("seed", "user"):  # user last so it overwrites seed (case flag and all)
        for row in conn.execute(
            "SELECT input_text, spoken, case_sensitive FROM lexicon WHERE origin = ?",
            (origin,),
        ):
            rows[row["input_text"]] = (row["spoken"], bool(row["case_sensitive"]))
    cs = {key: spoken for key, (spoken, sensitive) in rows.items() if sensitive}
    ci = {key: spoken for key, (spoken, sensitive) in rows.items() if not sensitive}
    return cs, ci


def reference_entries(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Seed + user rows as ``(input_text, formatted reference line)`` pairs so
    the pronunciation pass can filter the reference to terms present in a
    chunk. The huge ``base`` layer is excluded: it would blow the prompt
    context and is applied deterministically per-token instead."""

    entries: list[tuple[str, str]] = []
    for row in conn.execute(
        "SELECT input_text, spoken, notes FROM lexicon WHERE origin IN ('seed', 'user') "
        "ORDER BY origin DESC, input_text"
    ):
        line = f"- {row['input_text']} -> {row['spoken']}"
        if row["notes"]:  # disambiguation context for homographs etc.
            line += f"  ({row['notes']})"
        entries.append((row["input_text"], line))
    return entries


def reference_text(conn: sqlite3.Connection) -> str:
    """The full LLM pronunciation reference, one ``- input -> spoken`` line
    per seed/user term."""

    return "\n".join(line for _, line in reference_entries(conn))


def get_user_entries(conn: sqlite3.Connection) -> dict[str, dict]:
    """User rows as ``{input_text: {mode, spoken, case_sensitive}}``."""

    out: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT * FROM lexicon WHERE origin = 'user' ORDER BY input_text"
    ):
        out[row["input_text"]] = {
            "mode": row["mode"],
            "spoken": row["spoken"],
            "case_sensitive": bool(row["case_sensitive"]),
        }
    return out


def replace_user_entries(conn: sqlite3.Connection, entries: dict[str, dict]) -> None:
    """Replace all user rows with ``entries`` (read-only rows untouched)."""

    conn.execute("DELETE FROM lexicon WHERE origin = 'user'")
    insert_entries(conn, "user", entries, read_only=False)
    conn.commit()


# Rows per insert batch during a bulk import. With wal_autocheckpoint off for
# the import, the WAL can only shrink at an explicit checkpoint, so an
# unbatched 1.3M-row insert grew it past 10 GB for a 161 MB database (#126
# follow-up). Checkpointing between batches bounds peak WAL to roughly one
# batch's pages while keeping the big page cache and key-ordered writes.
_IMPORT_BATCH_ROWS = 50_000


def import_readonly(conn: sqlite3.Connection, origin: str, entries: dict[str, dict]) -> None:
    """Replace all read-only rows of ``origin`` (seed/base) with ``entries``.

    User rows are never touched. The insert is committed and checkpointed in
    batches rather than as one transaction: peak WAL matters more here than
    atomicity, because the version key is only written after a full import, so
    an interrupted import simply re-runs from the start on the next boot
    (leaving a partial read-only layer in the meantime, which degrades
    pronunciation slightly but corrupts nothing).
    """

    if origin not in ("seed", "base"):
        raise ValueError(f"import_readonly origin must be seed/base, got {origin}")
    conn.execute("DELETE FROM lexicon WHERE origin = ?", (origin,))
    insert_entries(conn, origin, entries, read_only=True, checkpoint_between_batches=True)


def insert_entries(
    conn: sqlite3.Connection,
    origin: str,
    entries: dict[str, dict],
    *,
    read_only: bool,
    checkpoint_between_batches: bool = False,
) -> None:
    # Sorted by key so the bulk import walks the (origin, input_text) PK and
    # the input_text index in order instead of random-writing B-tree pages;
    # on the 1.3M-row base layer that locality is a large I/O win (#126).
    rows = [
        (
            origin,
            key,
            key.casefold(),
            entry.get("mode", "override"),
            entry["spoken"],
            1 if entry.get("case_sensitive") else 0,
            float(entry.get("confidence", 1.0)),
            entry.get("source"),
            entry.get("notes"),
            1 if read_only else 0,
        )
        for key, entry in sorted(entries.items())
    ]
    statement = """
        INSERT OR REPLACE INTO lexicon
            (origin, input_text, input_fold, mode, spoken,
             case_sensitive, confidence, source, notes, read_only)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
    for start in range(0, len(rows), _IMPORT_BATCH_ROWS):
        batch = rows[start : start + _IMPORT_BATCH_ROWS]
        conn.executemany(statement, batch)
        if not checkpoint_between_batches:
            continue
        # TRUNCATE rather than PASSIVE: PASSIVE leaves the file at its high
        # water mark, which is the number this is trying to bound.
        with suppress(sqlite3.OperationalError):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        logger.debug(
            "Lexicon import progress",
            extra={
                "event": "lexicon_import_progress",
                "origin": origin,
                "rows_written": min(start + _IMPORT_BATCH_ROWS, len(rows)),
                "rows_total": len(rows),
            },
        )


WORD_TOKEN_RE = re.compile("[A-Za-z][A-Za-z'\u2019-]*")


def iter_entries(conn: sqlite3.Connection, scope: str = "user"):
    """Yield LexEntry rows for export. scope 'user' = editable rows only;
    'all' = the full table (user + seed + base), streamed in key order."""

    if scope == "user":
        where = "WHERE origin = 'user'"
    elif scope == "all":
        where = ""
    else:
        raise ValueError(f"scope must be user/all, got {scope}")
    cursor = conn.execute(f"SELECT * FROM lexicon {where} ORDER BY input_text")
    for row in cursor:
        yield _row_to_entry(row)


def counts_by_origin(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        row["origin"]: row["n"]
        for row in conn.execute("SELECT origin, COUNT(*) AS n FROM lexicon GROUP BY origin")
    }
