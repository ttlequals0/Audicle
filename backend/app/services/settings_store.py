"""Key/value settings store backed by the SQLite ``settings`` table.

Used for ``podcast:guid`` (stable feed identifier per the
Podcasting 2.0 spec). Subsequent phases will pile on runtime knobs
(retention overrides, auth tunables, etc).
"""

from __future__ import annotations

import secrets
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
# Monotonic counter bumped by the force-recreate endpoint. When > 0 it is
# appended to every episode <guid> in the feed so podcast apps treat the
# episodes as new and re-download them; the channel podcast:guid is rotated
# in lockstep. Absent/0 means the original (unsalted) guids.
FEED_GUID_EPOCH_KEY = "feed_guid_epoch"

# Auth (MinusPod pattern): the admin password bcrypt hash and the session
# signing secret live in the settings table, set/auto-generated at runtime
# rather than required as env vars. Presence of APP_PASSWORD_KEY enables auth;
# absence = open convenience mode.
APP_PASSWORD_KEY = "app_password"
SESSION_SECRET_KEY_NAME = "session_secret"

# Operator-editable config that used to live in bind-mounted files (now DB-backed
# so a deploy ships behavior via the image default and /data holds only media).
# Absence of a row means "use the packaged default".
CLEANUP_PROMPT_KEY = "cleanup_prompt"
SUMMARY_PROMPT_KEY = "summary_prompt"
PRONUNCIATION_PROMPT_KEY = "pronunciation_prompt"
PRONUNCIATION_KEY = "pronunciation_dict"


def get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def delete(conn: sqlite3.Connection, key: str) -> None:
    """Remove a setting row so the caller falls back to its packaged default."""

    conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    conn.commit()


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


def get_feed_guid_epoch(conn: sqlite3.Connection) -> int:
    """Current feed-guid epoch (0 when never rotated or unparseable)."""

    raw = get(conn, FEED_GUID_EPOCH_KEY)
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def rotate_feed_guids(conn: sqlite3.Connection, base_url: str) -> tuple[str, int]:
    """Force a new feed identity: bump the epoch and rotate the channel guid.

    The new channel ``podcast:guid`` stays a spec-shaped UUIDv5 (same namespace
    and canonical feed URL) but is salted with the new epoch so it differs from
    the prior value -- a plain re-derivation would reproduce the identical guid.
    Returns ``(new_podcast_guid, new_epoch)``.
    """

    new_epoch = get_feed_guid_epoch(conn) + 1
    salted = f"{_canonical_feed_url(base_url)}#{new_epoch}"
    new_guid = str(uuid.uuid5(_PODCAST_GUID_NAMESPACE, salted))
    set_(conn, PODCAST_GUID_KEY, new_guid)
    set_(conn, FEED_GUID_EPOCH_KEY, str(new_epoch))
    return new_guid, new_epoch


def get_or_init_session_secret(conn: sqlite3.Connection) -> str:
    """Return the persisted session signing secret, generating one on first use.

    Auto-generated and stored so SessionMiddleware has a stable key across
    restarts without requiring an env var. An explicit SESSION_SECRET_KEY env
    override is applied by the caller before falling back to this.
    """

    existing = get(conn, SESSION_SECRET_KEY_NAME)
    if existing:
        return existing
    fresh = secrets.token_urlsafe(64)
    # First-writer-wins, atomic at the SQLite level: INSERT OR IGNORE never
    # overwrites an existing row, so two worker processes racing on a fresh DB
    # converge on a single secret instead of each signing cookies with its own
    # key (which logs users out on ~half their requests under round-robin). Do
    # NOT use set_(): its ON CONFLICT DO UPDATE is overwrite semantics. Re-SELECT
    # to return whichever value actually landed.
    conn.execute(
        """
        INSERT OR IGNORE INTO settings (key, value, updated_at)
        VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        """,
        (SESSION_SECRET_KEY_NAME, fresh),
    )
    conn.commit()
    return get(conn, SESSION_SECRET_KEY_NAME) or fresh


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
