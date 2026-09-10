from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import Settings

_FEED_FIELDS = (
    "BASE_URL",
    "FEED_TITLE",
    "FEED_DESCRIPTION",
    "FEED_AUTHOR",
    "FEED_EMAIL",
    "FEED_LANGUAGE",
    "FEED_CATEGORY",
    "FEED_EXPLICIT",
    "FEED_ARTWORK_URL",
    "FEED_AUTH_ENABLED",
    "FEED_AUTH_KEY",
    "DEFAULT_ARTWORK_URL",
)


@dataclass(frozen=True)
class FeedIdentity:
    revision: int
    modified_at: datetime
    settings_fingerprint: str
    ims_ambiguous: bool


def _modification_state(conn: sqlite3.Connection) -> tuple[str, int]:
    row = conn.execute("SELECT revision, modified_at FROM feed_state WHERE id=1").fetchone()
    now = datetime.now(UTC)
    if row is not None:
        previous = datetime.fromisoformat(row["modified_at"]).astimezone(UTC)
        ambiguous = int(
            int(row["revision"]) > 0
            and now.replace(microsecond=0) <= previous.replace(microsecond=0)
        )
    else:
        ambiguous = 0
    return now.isoformat(timespec="microseconds"), ambiguous


def fingerprint(settings: Settings) -> str:
    return hashlib.sha256(
        json.dumps(
            {field: getattr(settings, field) for field in _FEED_FIELDS},
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()


def bump(conn: sqlite3.Connection, settings: Settings | None = None) -> None:
    if settings is None:
        modified_at, ambiguous = _modification_state(conn)
        conn.execute(
            "UPDATE feed_state SET revision=revision+1, modified_at=?, ims_ambiguous=? WHERE id=1",
            (modified_at, ambiguous),
        )
    else:
        modified_at, ambiguous = _modification_state(conn)
        conn.execute(
            "UPDATE feed_state SET revision=revision+1, modified_at=?, settings_fingerprint=?, "
            "ims_ambiguous=? "
            "WHERE id=1",
            (modified_at, fingerprint(settings), ambiguous),
        )


def identity(conn: sqlite3.Connection, settings: Settings) -> FeedIdentity:
    fingerprint_value = fingerprint(settings)
    existing = current(conn)
    if existing.settings_fingerprint == fingerprint_value:
        return existing
    modified_at, ambiguous = _modification_state(conn)
    if not existing.settings_fingerprint:
        ambiguous = 0
    conn.execute(
        "UPDATE feed_state SET revision=revision+1, modified_at=?, settings_fingerprint=?, "
        "ims_ambiguous=? WHERE id=1 AND settings_fingerprint<>?",
        (modified_at, fingerprint_value, ambiguous, fingerprint_value),
    )
    return current(conn)


def current(conn: sqlite3.Connection) -> FeedIdentity:
    row = conn.execute(
        "SELECT revision, modified_at, settings_fingerprint, ims_ambiguous FROM feed_state WHERE id=1"
    ).fetchone()
    return FeedIdentity(
        revision=int(row["revision"]),
        modified_at=datetime.fromisoformat(row["modified_at"]).astimezone(UTC),
        settings_fingerprint=str(row["settings_fingerprint"]),
        ims_ambiguous=bool(row["ims_ambiguous"]),
    )
