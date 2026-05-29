"""Operator-tunable settings that override env defaults at request time.

Only an allowlisted subset of ``Settings`` fields is exposed -- the rest are
infrastructure-level (DB path, secret keys) and would be footguns to flip
without a restart. The resolver coerces stored strings back to the field's
declared type via ``Settings.__class__.model_fields`` introspection.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

# Operator-tunable subset. Any field not in this set returns 400 on PUT and
# is invisible on GET; cosmetic/runtime tuning lives here, secrets and
# infrastructure paths do not.
ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "FEED_TITLE",
        "FEED_DESCRIPTION",
        "FEED_AUTHOR",
        "FEED_EMAIL",
        "FEED_LANGUAGE",
        "FEED_CATEGORY",
        "FEED_EXPLICIT",
        "FEED_ARTWORK_URL",
        "RETENTION_DAYS",
        "TTS_CHUNK_TARGET_WORDS",
        "TTS_CHUNK_MAX_WORDS",
        "TTS_CHUNK_SILENCE_MS",
        "RSS_CACHE_MAX_AGE_SECONDS",
        "MIN_CLEANUP_CHARS",
        "MAX_PROMPT_LENGTH_BYTES",
    }
)


def get_all(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM runtime_settings").fetchall()
    return {row["key"]: row["value"] for row in rows if row["key"] in ALLOWED_KEYS}


def set_value(conn: sqlite3.Connection, key: str, value: Any) -> None:
    if key not in ALLOWED_KEYS:
        raise KeyError(f"{key} is not an operator-tunable setting")
    serialized = _serialize(value)
    conn.execute(
        """
        INSERT INTO runtime_settings (key, value, updated_at)
        VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, serialized),
    )
    conn.commit()


def delete(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM runtime_settings WHERE key = ?", (key,))
    conn.commit()


def _serialize(value: Any) -> str:
    if isinstance(value, bool | int | float):
        return json.dumps(value)
    if isinstance(value, str):
        return value
    return json.dumps(value)
