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
    ipa TEXT,
    case_sensitive INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 1.0,
    source TEXT,
    notes TEXT,
    read_only INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (origin, input_text)
);
"""
_CREATE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_lexicon_fold ON lexicon(input_fold);"


@dataclass(frozen=True)
class LexEntry:
    origin: str
    input_text: str
    mode: str
    spoken: str
    ipa: str | None
    case_sensitive: bool
    confidence: float
    source: str | None
    notes: str | None
    read_only: bool


def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(CREATE_TABLE_SQL)
    conn.execute(_CREATE_INDEX_SQL)


def _row_to_entry(row: sqlite3.Row) -> LexEntry:
    return LexEntry(
        origin=row["origin"],
        input_text=row["input_text"],
        mode=row["mode"],
        spoken=row["spoken"],
        ipa=row["ipa"],
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
    rows = conn.execute(
        """
        SELECT * FROM lexicon
        WHERE (case_sensitive = 1 AND input_text = ?)
           OR (case_sensitive = 0 AND input_fold = ?)
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


def apply_pairs(conn: sqlite3.Connection) -> dict[str, str]:
    """``{input_text: spoken}`` for user + seed rows, user winning on collision.

    Feeds the longest-key-first regex substitution (multi-word phrases). The
    large base layer is applied per-token via :func:`lookup`, not here.
    """

    pairs: dict[str, str] = {}
    for origin in ("seed", "user"):  # user last so it overwrites seed
        for row in conn.execute(
            "SELECT input_text, spoken FROM lexicon WHERE origin = ?", (origin,)
        ):
            pairs[row["input_text"]] = row["spoken"]
    return pairs


def word_keep_set(conn: sqlite3.Connection) -> set[str]:
    """Inputs whose mode is 'word' -- acronyms read as words (NASA), so the
    deterministic speller must not letter-spell them."""

    return {
        row["input_text"]
        for row in conn.execute("SELECT input_text FROM lexicon WHERE mode = 'word'")
    }


def reference_text(conn: sqlite3.Connection) -> str:
    """Format seed + user rows as the LLM pronunciation reference.

    One ``- input -> spoken`` line per term. The huge ``base`` layer is excluded:
    it would blow the prompt context and is applied deterministically per-token
    instead.
    """

    lines = []
    for row in conn.execute(
        "SELECT input_text, spoken, notes FROM lexicon WHERE origin IN ('seed', 'user') "
        "ORDER BY origin DESC, input_text"
    ):
        line = f"- {row['input_text']} -> {row['spoken']}"
        if row["notes"]:  # disambiguation context for homographs etc.
            line += f"  ({row['notes']})"
        lines.append(line)
    return "\n".join(lines)


def get_user_entries(conn: sqlite3.Connection) -> dict[str, dict]:
    """User rows as ``{input_text: {mode, spoken, ipa, case_sensitive}}``."""

    out: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT * FROM lexicon WHERE origin = 'user' ORDER BY input_text"
    ):
        out[row["input_text"]] = {
            "mode": row["mode"],
            "spoken": row["spoken"],
            "ipa": row["ipa"],
            "case_sensitive": bool(row["case_sensitive"]),
        }
    return out


def replace_user_entries(conn: sqlite3.Connection, entries: dict[str, dict]) -> None:
    """Replace all user rows with ``entries`` (read-only rows untouched)."""

    conn.execute("DELETE FROM lexicon WHERE origin = 'user'")
    insert_entries(conn, "user", entries, read_only=False)
    conn.commit()


def import_readonly(conn: sqlite3.Connection, origin: str, entries: dict[str, dict]) -> None:
    """Replace all read-only rows of ``origin`` (seed/base) with ``entries``.

    User rows are never touched. Caller commits (so the versioned import stays
    inside one transaction).
    """

    if origin not in ("seed", "base"):
        raise ValueError(f"import_readonly origin must be seed/base, got {origin}")
    conn.execute("DELETE FROM lexicon WHERE origin = ?", (origin,))
    insert_entries(conn, origin, entries, read_only=True)


def insert_entries(
    conn: sqlite3.Connection, origin: str, entries: dict[str, dict], *, read_only: bool
) -> None:
    conn.executemany(
        """
        INSERT OR REPLACE INTO lexicon
            (origin, input_text, input_fold, mode, spoken, ipa,
             case_sensitive, confidence, source, notes, read_only)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                origin,
                key,
                key.casefold(),
                entry.get("mode", "override"),
                entry["spoken"],
                entry.get("ipa"),
                1 if entry.get("case_sensitive") else 0,
                float(entry.get("confidence", 1.0)),
                entry.get("source"),
                entry.get("notes"),
                1 if read_only else 0,
            )
            for key, entry in entries.items()
        ],
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
