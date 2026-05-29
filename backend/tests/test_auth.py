from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import bcrypt
import pytest
from app.config import get_settings
from app.core import database
from app.services import auth


@pytest.fixture
def settings_with_auth(monkeypatch: pytest.MonkeyPatch, env: Path):
    """Common fixture: enable auth, set ADMIN_PASSWORD_HASH to a known
    bcrypt hash, and provide a SESSION_SECRET_KEY so the validator passes."""

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        bcrypt.hashpw(b"correct-horse-battery", bcrypt.gensalt()).decode("ascii"),
    )
    monkeypatch.setenv("SESSION_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("LOCKOUT_MAX_FAILED_ATTEMPTS", "3")
    monkeypatch.setenv("LOCKOUT_WINDOW_SECONDS", "900")
    get_settings.cache_clear()
    return get_settings()


def _conn(env: Path):
    database.run_migrations(env)
    return database.connect(database.db_path(env))


def test_hash_password_round_trips() -> None:
    h = auth.hash_password("hunter2")
    assert bcrypt.checkpw(b"hunter2", h.encode("ascii"))


def test_verify_credentials_succeeds_with_correct_password(env: Path, settings_with_auth) -> None:
    conn = _conn(env)
    try:
        auth.verify_credentials(
            conn,
            username="admin",
            password="correct-horse-battery",
            settings=settings_with_auth,
        )
    finally:
        conn.close()


def test_verify_credentials_rejects_wrong_password(env: Path, settings_with_auth) -> None:
    conn = _conn(env)
    try:
        with pytest.raises(auth.InvalidCredentialsError):
            auth.verify_credentials(
                conn,
                username="admin",
                password="wrong",
                settings=settings_with_auth,
            )
    finally:
        conn.close()


def test_verify_credentials_rejects_wrong_username(env: Path, settings_with_auth) -> None:
    conn = _conn(env)
    try:
        with pytest.raises(auth.InvalidCredentialsError):
            auth.verify_credentials(
                conn,
                username="not-admin",
                password="correct-horse-battery",
                settings=settings_with_auth,
            )
    finally:
        conn.close()


def test_lockout_triggers_after_threshold(env: Path, settings_with_auth) -> None:
    """3 failures (per the fixture override) opens a lockout window so the
    4th attempt raises LockedOutError instead of InvalidCredentialsError."""

    conn = _conn(env)
    try:
        for _ in range(3):
            with pytest.raises(auth.InvalidCredentialsError):
                auth.verify_credentials(
                    conn,
                    username="admin",
                    password="wrong",
                    settings=settings_with_auth,
                )
        with pytest.raises(auth.LockedOutError):
            auth.verify_credentials(
                conn,
                username="admin",
                password="correct-horse-battery",
                settings=settings_with_auth,
            )
    finally:
        conn.close()


def test_successful_login_clears_prior_lockout(env: Path, settings_with_auth) -> None:
    """A bad password followed by a manual ``DELETE FROM auth_lockout``
    must let the operator log in immediately (operator recovery path)."""

    conn = _conn(env)
    try:
        with pytest.raises(auth.InvalidCredentialsError):
            auth.verify_credentials(
                conn,
                username="admin",
                password="wrong",
                settings=settings_with_auth,
            )
        # Operator clears the lockout row.
        conn.execute("DELETE FROM auth_lockout")
        conn.commit()
        # Next call with the correct password succeeds.
        auth.verify_credentials(
            conn,
            username="admin",
            password="correct-horse-battery",
            settings=settings_with_auth,
        )
        # And the lockout row is absent (would be auto-cleared too).
        row = conn.execute("SELECT * FROM auth_lockout WHERE identifier = 'admin'").fetchone()
        assert row is None
    finally:
        conn.close()


def test_lockout_window_expires(
    env: Path, settings_with_auth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After ``LOCKOUT_WINDOW_SECONDS`` the lockout no longer blocks."""

    conn = _conn(env)
    try:
        # Manually seed a lockout row whose window already expired.
        past = (datetime.now(UTC) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """
            INSERT INTO auth_lockout (identifier, failed_attempts,
                last_attempt_at, lockout_until)
            VALUES ('admin', 5, ?, ?)
            """,
            (past, past),
        )
        conn.commit()
        # Correct password works because the window expired.
        auth.verify_credentials(
            conn,
            username="admin",
            password="correct-horse-battery",
            settings=settings_with_auth,
        )
    finally:
        conn.close()


def test_verify_with_malformed_hash_returns_invalid_creds(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator mis-paste in ADMIN_PASSWORD_HASH must surface as 401,
    not a 500."""

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "not-a-bcrypt-hash")
    monkeypatch.setenv("SESSION_SECRET_KEY", "x" * 64)
    get_settings.cache_clear()
    settings = get_settings()
    conn = _conn(env)
    try:
        with pytest.raises(auth.InvalidCredentialsError):
            auth.verify_credentials(
                conn,
                username="admin",
                password="anything",
                settings=settings,
            )
    finally:
        conn.close()


def test_username_is_case_insensitive(env: Path, settings_with_auth) -> None:
    conn = _conn(env)
    try:
        # Uppercase username should still match the lower-cased identifier.
        auth.verify_credentials(
            conn,
            username="ADMIN",
            password="correct-horse-battery",
            settings=settings_with_auth,
        )
    finally:
        conn.close()
