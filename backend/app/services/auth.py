"""Single-admin authentication for the admin UI.

Phase 9 ships a single ``ADMIN_USERNAME`` whose bcrypt hash lives in
``ADMIN_PASSWORD_HASH``. The login flow:

1. Operator POSTs username/password to ``/api/v1/auth/login``.
2. ``services.auth.verify_credentials`` checks the bcrypt hash AND that the
   identifier isn't currently locked out.
3. On success the session cookie is set (signed by
   ``SESSION_SECRET_KEY``) and any prior lockout row is cleared.
4. On failure the lockout counter is bumped; ``LOCKOUT_MAX_FAILED_ATTEMPTS``
   triggers a ``LOCKOUT_WINDOW_SECONDS`` ban window during which the next
   login attempt returns 423 Locked.

The auth_lockout table is the source of truth for the lockout window; the
verifier always re-reads it (no in-memory cache) so a manual
``DELETE FROM auth_lockout`` immediately recovers a locked-out account.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import bcrypt

from app.config import Settings

logger = logging.getLogger("app.services.auth")


class AuthError(Exception):
    """Base class for login-flow failures."""


class InvalidCredentialsError(AuthError):
    """Username or password did not match."""


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
    """Helper for operators to generate the env-var hash."""

    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def _verify_password(plaintext: str, stored_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), stored_hash.encode("ascii"))
    except (ValueError, TypeError):
        # Malformed hash (operator mis-pasted the env var). Treat as a
        # non-match rather than 500 so the admin gets a normal 401.
        logger.warning(
            "ADMIN_PASSWORD_HASH appears malformed",
            extra={"event": "auth_hash_malformed"},
        )
        return False


def verify_credentials(
    conn: sqlite3.Connection,
    *,
    username: str,
    password: str,
    settings: Settings,
) -> None:
    """Authenticate ``username``/``password`` against the admin config.

    Raises :class:`LockedOutError` if the identifier is currently in the
    lockout window, or :class:`InvalidCredentialsError` on a mismatch
    (which also bumps the lockout counter).
    """

    if not settings.ADMIN_PASSWORD_HASH:
        raise AuthError("ADMIN_PASSWORD_HASH is not set")

    identifier = username.strip().lower()
    state = _get_lockout(conn, identifier)
    now = datetime.now(UTC)
    if state and state.locked_until and state.locked_until > now:
        raise LockedOutError(state.locked_until)

    expected_user = settings.ADMIN_USERNAME.strip().lower()
    if identifier != expected_user or not _verify_password(password, settings.ADMIN_PASSWORD_HASH):
        _register_failed_attempt(conn, identifier, settings)
        raise InvalidCredentialsError("invalid username or password")

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
    now = datetime.now(UTC)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    state = _get_lockout(conn, identifier)
    failed = (state.failed_attempts if state else 0) + 1
    locked_until: str | None = None
    if failed >= settings.LOCKOUT_MAX_FAILED_ATTEMPTS:
        locked_until = (now + timedelta(seconds=settings.LOCKOUT_WINDOW_SECONDS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    conn.execute(
        """
        INSERT INTO auth_lockout (identifier, failed_attempts, last_attempt_at, lockout_until)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(identifier) DO UPDATE SET
            failed_attempts = excluded.failed_attempts,
            last_attempt_at = excluded.last_attempt_at,
            lockout_until   = excluded.lockout_until
        """,
        (identifier, failed, now_iso, locked_until),
    )
    conn.commit()
    if locked_until:
        logger.warning(
            "Lockout triggered",
            extra={
                "event": "auth_lockout_triggered",
                "identifier": identifier,
                "failed_attempts": failed,
                "locked_until": locked_until,
            },
        )


def _clear_lockout(conn: sqlite3.Connection, identifier: str) -> None:
    conn.execute("DELETE FROM auth_lockout WHERE identifier = ?", (identifier,))
    conn.commit()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None
