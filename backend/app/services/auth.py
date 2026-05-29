"""Password-only admin authentication for the admin UI (MinusPod pattern).

The admin password bcrypt hash lives in the ``settings`` table (set via the
UI), not an env var. No password set = open convenience mode. The login flow:

1. Operator POSTs ``{password}`` to ``/api/v1/auth/login``.
2. ``services.auth.verify_login`` checks the bcrypt hash AND that the client
   IP isn't currently locked out.
3. On success the session cookie is set and any prior lockout row is cleared.
4. On failure the lockout counter for that IP is bumped;
   ``LOCKOUT_MAX_FAILED_ATTEMPTS`` triggers a ``LOCKOUT_WINDOW_SECONDS`` ban
   window during which the next attempt returns 423 Locked.

The auth_lockout table is the source of truth for the lockout window; the
verifier always re-reads it (no in-memory cache) so a manual
``DELETE FROM auth_lockout`` immediately recovers a locked-out IP.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import bcrypt

from app.config import Settings
from app.services import settings_store

# Precomputed valid-shape bcrypt hash whose checkpw of any input is False.
# verify_login runs bcrypt against this even when no password is stored so the
# wall-clock cost is constant regardless of whether a password is set.
_DUMMY_HASH = "$2b$12$abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.123"

# Minimum length enforced when setting a password via the UI.
MIN_PASSWORD_LENGTH = 8

logger = logging.getLogger("app.services.auth")


class AuthError(Exception):
    """Base class for login-flow failures."""


class InvalidCredentialsError(AuthError):
    """The password did not match."""


class LockedOutError(AuthError):
    """The identifier is currently in a lockout window."""

    def __init__(self, locked_until: datetime) -> None:
        super().__init__(f"locked until {locked_until.isoformat()}")
        self.locked_until = locked_until


@dataclass(frozen=True)
class LockoutState:
    failed_attempts: int
    last_attempt_at: datetime
    locked_until: datetime | None


def hash_password(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def _verify_password(plaintext: str, stored_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), stored_hash.encode("ascii"))
    except (ValueError, TypeError):
        # Malformed stored hash. Treat as a non-match rather than 500.
        logger.warning("stored password hash appears malformed", extra={"event": "auth_hash_malformed"})
        return False


def is_password_set(conn: sqlite3.Connection) -> bool:
    """True when an admin password is configured (auth on); False = convenience mode."""

    return bool(settings_store.get(conn, settings_store.APP_PASSWORD_KEY))


def set_password(conn: sqlite3.Connection, plaintext: str) -> None:
    """Store the bcrypt hash of ``plaintext`` as the admin password."""

    settings_store.set_(conn, settings_store.APP_PASSWORD_KEY, hash_password(plaintext))


def clear_password(conn: sqlite3.Connection) -> None:
    """Remove the admin password (revert to open convenience mode)."""

    conn.execute("DELETE FROM settings WHERE key = ?", (settings_store.APP_PASSWORD_KEY,))
    conn.commit()


def verify_login(
    conn: sqlite3.Connection,
    *,
    password: str,
    identifier: str,
    settings: Settings,
) -> None:
    """Verify ``password`` for the client ``identifier`` (its IP).

    Raises :class:`LockedOutError` if the IP is in its lockout window, or
    :class:`InvalidCredentialsError` on a mismatch (which bumps the counter).
    """

    state = _get_lockout(conn, identifier)
    now = datetime.now(UTC)
    if state and state.locked_until and state.locked_until > now:
        raise LockedOutError(state.locked_until)

    stored = settings_store.get(conn, settings_store.APP_PASSWORD_KEY)
    # Always run bcrypt (dummy hash when unset) so timing doesn't reveal whether
    # a password is configured; an unset password never authenticates.
    if not stored or not _verify_password(password, stored or _DUMMY_HASH):
        _register_failed_attempt(conn, identifier, settings)
        raise InvalidCredentialsError("invalid password")

    _clear_lockout(conn, identifier)


def _get_lockout(conn: sqlite3.Connection, identifier: str) -> LockoutState | None:
    row = conn.execute(
        """
        SELECT failed_attempts, last_attempt_at, lockout_until
        FROM auth_lockout WHERE identifier = ?
        """,
        (identifier,),
    ).fetchone()
    if row is None:
        return None
    return LockoutState(
        failed_attempts=row["failed_attempts"],
        last_attempt_at=_parse_iso(row["last_attempt_at"]) or datetime.now(UTC),
        locked_until=_parse_iso(row["lockout_until"]) if row["lockout_until"] else None,
    )


def _register_failed_attempt(conn: sqlite3.Connection, identifier: str, settings: Settings) -> None:
    """Atomically bump the failed-attempt counter and arm the lockout window.

    The previous implementation read the row in Python and wrote the new
    value back, which raced under WEB_WORKERS=2 (two concurrent failed
    logins could both read N and write N+1, defeating the threshold). The
    single SQL statement below does the increment + lockout decision inside
    the engine so SQLite's per-row write lock makes the operation
    serializable.
    """

    now = datetime.now(UTC)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    locked_until_iso = (now + timedelta(seconds=settings.LOCKOUT_WINDOW_SECONDS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    threshold = settings.LOCKOUT_MAX_FAILED_ATTEMPTS
    conn.execute(
        """
        INSERT INTO auth_lockout (
            identifier, failed_attempts, last_attempt_at, lockout_until
        )
        VALUES (?, 1, ?, NULL)
        ON CONFLICT(identifier) DO UPDATE SET
            failed_attempts = failed_attempts + 1,
            last_attempt_at = excluded.last_attempt_at,
            lockout_until = CASE
                WHEN failed_attempts + 1 >= ?
                THEN ?
                ELSE NULL
            END
        """,
        (identifier, now_iso, threshold, locked_until_iso),
    )
    conn.commit()
    row = conn.execute(
        "SELECT failed_attempts, lockout_until FROM auth_lockout WHERE identifier = ?",
        (identifier,),
    ).fetchone()
    if row is not None and row["lockout_until"] is not None:
        logger.warning(
            "Lockout triggered",
            extra={
                "event": "auth_lockout_triggered",
                "identifier": identifier,
                "failed_attempts": row["failed_attempts"],
                "locked_until": row["lockout_until"],
            },
        )


def _clear_lockout(conn: sqlite3.Connection, identifier: str) -> None:
    conn.execute("DELETE FROM auth_lockout WHERE identifier = ?", (identifier,))
    conn.commit()


# Module-level alias keeps the existing call sites stable while routing
# through the canonical helper. Future code should import ``parse_iso``
# directly from ``app.core.timestamps``.
from app.core.timestamps import parse_iso as _parse_iso  # noqa: E402
