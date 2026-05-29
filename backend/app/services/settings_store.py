"""Key/value settings store backed by the SQLite ``settings`` table.

Used for ``podcast:guid`` (stable feed identifier per the
Podcasting 2.0 spec). Subsequent phases will pile on runtime knobs
(retention overrides, auth tunables, etc).
"""

from __future__ import annotations

import sqlite3
import uuid
from urllib.parse import urlsplit

# The Podcasting 2.0 ``podcast:guid`` spec
# (https://github.com/Podcastindex-org/podcast-namespace/blob/main/docs/1.0.md#guid)
# requires UUIDv5 derived from the feed URL with the scheme stripped and
# trailing slash removed, using this specific namespace. Using
# ``uuid.NAMESPACE_URL`` would produce a value that no other PC2-aware
# tool computes for the same feed URL, defeating the spec's cross-aggregator
# deduplication.
_PODCAST_GUID_NAMESPACE = uuid.UUID("ead4c236-bf58-58c6-a2c6-a6b28d128cb6")
PODCAST_GUID_KEY = "podcast_guid"


def get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def set_(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, value),
    )
    conn.commit()


def get_or_init_podcast_guid(conn: sqlite3.Connection, base_url: str) -> str:
    """Return the stable ``podcast:guid``, initializing on first call.

    First call derives a UUIDv5 from ``BASE_URL`` and persists it; subsequent
    calls return the persisted value verbatim. Per the Podcasting 2.0 spec
    the GUID must remain stable even if the feed URL or hosting provider
    changes, so storing it (rather than recomputing every render) is the
    correct shape.
    """

    existing = get(conn, PODCAST_GUID_KEY)
    if existing:
        return existing
    fresh = str(uuid.uuid5(_PODCAST_GUID_NAMESPACE, _canonical_feed_url(base_url)))
    set_(conn, PODCAST_GUID_KEY, fresh)
    return fresh


def _canonical_feed_url(base_url: str) -> str:
    """Strip scheme and trailing slash per the PC2 guid derivation rule.

    ``https://example.com/`` -> ``example.com``;
    ``http://example.com/path/`` -> ``example.com/path``.
    """

    parts = urlsplit(base_url)
    host = parts.netloc or parts.path  # tolerate inputs lacking scheme
    path = parts.path if parts.netloc else ""
    canonical = (host + path).rstrip("/")
    return canonical
