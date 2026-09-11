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

import asyncio
import concurrent.futures
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
_DUMMY_HASH = "$2b$12$piQLO7tCxEv4uc0gVUKW6.stew3JJV6ec4nXivS2v.76ImP8Anmie"

# Minimum length enforced when setting a password via the UI.
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_BYTES = 72
AUTH_GENERATION_KEY = "auth_generation"
_BCRYPT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="audicle-bcrypt"
)

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


class CredentialsChangedError(AuthError):
    pass


@dataclass(frozen=True)
class LockoutState:
    failed_attempts: int
    last_attempt_at: datetime
    locked_until: datetime | None


def hash_password(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def password_fits_bcrypt(plaintext: str) -> bool:
    return len(plaintext.encode("utf-8")) <= MAX_PASSWORD_BYTES


def auth_generation(conn: sqlite3.Connection) -> int:
    raw = settings_store.get(conn, AUTH_GENERATION_KEY)
    try:
        return int(raw or 0)
    except ValueError:
        return 0


def session_is_current(conn: sqlite3.Connection, value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get("user") == "admin"
        and value.get("generation") == auth_generation(conn)
    )


def revoke_all_sessions(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        generation = auth_generation(conn) + 1
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (AUTH_GENERATION_KEY, str(generation)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _verify_password(plaintext: str, stored_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), stored_hash.encode("ascii"))
    except (ValueError, TypeError):
        # Malformed stored hash. Treat as a non-match rather than 500.
        logger.warning(
            "stored password hash appears malformed", extra={"event": "auth_hash_malformed"}
        )
        return False


def is_password_set(conn: sqlite3.Connection) -> bool:
    """True when an admin password is configured (auth on); False = convenience mode."""

    return bool(settings_store.get(conn, settings_store.APP_PASSWORD_KEY))


def set_password(conn: sqlite3.Connection, plaintext: str) -> int:
    """Store the bcrypt hash of ``plaintext`` as the admin password."""

    return _store_password(conn, hash_password(plaintext))


def _store_password(
    conn: sqlite3.Connection,
    password_hash: str | None,
    expected: tuple[str | None, int] | None = None,
) -> int:
    conn.execute("BEGIN IMMEDIATE")
    try:
        generation = auth_generation(conn) + 1
        if (
            expected is not None
            and (settings_store.get(conn, settings_store.APP_PASSWORD_KEY), generation - 1)
            != expected
        ):
            raise CredentialsChangedError("credentials changed during password update")
        if password_hash is None:
            conn.execute("DELETE FROM settings WHERE key = ?", (settings_store.APP_PASSWORD_KEY,))
        else:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (settings_store.APP_PASSWORD_KEY, password_hash),
            )
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (AUTH_GENERATION_KEY, str(generation)),
        )
        conn.commit()
        return generation
    except Exception:
        conn.rollback()
        raise


def clear_password(
    conn: sqlite3.Connection,
    expected_generation: int | None = None,
    *,
    require_unset: bool = False,
) -> int:
    """Remove the admin password (revert to open convenience mode)."""

    expected = None
    if expected_generation is not None or require_unset:
        expected = (
            settings_store.get(conn, settings_store.APP_PASSWORD_KEY),
            expected_generation if expected_generation is not None else auth_generation(conn),
        )
    if require_unset and expected is not None and expected[0] is not None:
        raise CredentialsChangedError("credentials changed during password update")
    return _store_password(conn, None, expected)


async def set_password_async(
    conn: sqlite3.Connection,
    plaintext: str,
    expected_generation: int | None = None,
    *,
    require_unset: bool = False,
) -> int:
    expected = (
        settings_store.get(conn, settings_store.APP_PASSWORD_KEY),
        auth_generation(conn),
    )
    if expected_generation is not None and expected[1] != expected_generation:
        raise CredentialsChangedError("credentials changed during password update")
    if require_unset and expected[0] is not None:
        raise CredentialsChangedError("credentials changed during password update")
    loop = asyncio.get_running_loop()
    password_hash = await loop.run_in_executor(_BCRYPT_EXECUTOR, hash_password, plaintext)
    return _store_password(conn, password_hash, expected)


async def verify_login_async(
    conn: sqlite3.Connection, *, password: str, identifier: str, settings: Settings
) -> int:
    state = _get_lockout(conn, identifier)
    now = datetime.now(UTC)
    if state and state.locked_until and state.locked_until > now:
        raise LockedOutError(state.locked_until)
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE key IN (?, ?)",
        (settings_store.APP_PASSWORD_KEY, AUTH_GENERATION_KEY),
    ).fetchall()
    snapshot = {row["key"]: row["value"] for row in rows}
    stored = snapshot.get(settings_store.APP_PASSWORD_KEY)
    try:
        generation = int(snapshot.get(AUTH_GENERATION_KEY, "0"))
    except ValueError:
        generation = 0
    loop = asyncio.get_running_loop()
    matched = await loop.run_in_executor(
        _BCRYPT_EXECUTOR, _verify_password, password, stored or _DUMMY_HASH
    )
    if not stored or not matched:
        _register_failed_attempt(conn, identifier, settings)
        raise InvalidCredentialsError("invalid password")
    if (
        settings_store.get(conn, settings_store.APP_PASSWORD_KEY) != stored
        or auth_generation(conn) != generation
    ):
        raise InvalidCredentialsError("credentials changed during login")
    _clear_lockout(conn, identifier)
    return generation


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
