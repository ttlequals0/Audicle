"""Single-admin authentication for the admin UI.

A single ``ADMIN_USERNAME`` whose bcrypt hash lives in
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

import hmac
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import bcrypt

from app.config import Settings

# Precomputed valid-shape bcrypt hash whose checkpw of any input is False.
# Verify_credentials runs bcrypt even on unknown usernames against this hash
# so the wall-clock cost of a login attempt is constant regardless of
# whether the username exists -- closes the timing oracle described by the
# code-review pass.
_DUMMY_HASH = "$2b$12$abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.123"

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
    # Always run bcrypt -- against the dummy hash for an unknown username --
    # so the response time doesn't reveal whether the username matched.
    # ``hmac.compare_digest`` is constant-time over the strings.
    user_ok = hmac.compare_digest(identifier, expected_user)
    pw_ok = _verify_password(
        password,
        settings.ADMIN_PASSWORD_HASH if user_ok else _DUMMY_HASH,
    )
    if not (user_ok and pw_ok):
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
