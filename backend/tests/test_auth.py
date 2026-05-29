from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import bcrypt
import pytest
from app.config import get_settings
from app.core import database
from app.services import auth

IP = "203.0.113.7"


@pytest.fixture
def settings_(monkeypatch: pytest.MonkeyPatch, env: Path):
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


def test_password_set_lifecycle(env: Path) -> None:
    conn = _conn(env)
    try:
        assert auth.is_password_set(conn) is False
        auth.set_password(conn, "correct-horse-battery")
        assert auth.is_password_set(conn) is True
        auth.clear_password(conn)
        assert auth.is_password_set(conn) is False
    finally:
        conn.close()


def test_verify_login_succeeds_with_correct_password(env: Path, settings_) -> None:
    conn = _conn(env)
    try:
        auth.set_password(conn, "correct-horse-battery")
        auth.verify_login(
            conn, password="correct-horse-battery", identifier=IP, settings=settings_
        )
    finally:
        conn.close()


def test_verify_login_rejects_wrong_password(env: Path, settings_) -> None:
    conn = _conn(env)
    try:
        auth.set_password(conn, "correct-horse-battery")
        with pytest.raises(auth.InvalidCredentialsError):
            auth.verify_login(conn, password="wrong", identifier=IP, settings=settings_)
    finally:
        conn.close()


def test_verify_login_rejects_when_no_password_set(env: Path, settings_) -> None:
    """With no password stored, every login attempt is invalid (the endpoint
    handles convenience mode separately; verify_login never grants access)."""

    conn = _conn(env)
    try:
        with pytest.raises(auth.InvalidCredentialsError):
            auth.verify_login(conn, password="anything", identifier=IP, settings=settings_)
    finally:
        conn.close()


def test_lockout_triggers_after_threshold(env: Path, settings_) -> None:
    conn = _conn(env)
    try:
        auth.set_password(conn, "correct-horse-battery")
        for _ in range(3):
            with pytest.raises(auth.InvalidCredentialsError):
                auth.verify_login(conn, password="wrong", identifier=IP, settings=settings_)
        with pytest.raises(auth.LockedOutError):
            auth.verify_login(
                conn, password="correct-horse-battery", identifier=IP, settings=settings_
            )
    finally:
        conn.close()


def test_lockout_is_keyed_per_ip(env: Path, settings_) -> None:
    """A locked-out IP must not block a different client IP."""

    conn = _conn(env)
    try:
        auth.set_password(conn, "correct-horse-battery")
        for _ in range(3):
            with pytest.raises(auth.InvalidCredentialsError):
                auth.verify_login(conn, password="wrong", identifier=IP, settings=settings_)
        # Different IP is unaffected.
        auth.verify_login(
            conn, password="correct-horse-battery", identifier="198.51.100.2", settings=settings_
        )
    finally:
        conn.close()


def test_successful_login_clears_prior_lockout(env: Path, settings_) -> None:
    conn = _conn(env)
    try:
        auth.set_password(conn, "correct-horse-battery")
        with pytest.raises(auth.InvalidCredentialsError):
            auth.verify_login(conn, password="wrong", identifier=IP, settings=settings_)
        conn.execute("DELETE FROM auth_lockout")
        conn.commit()
        auth.verify_login(
            conn, password="correct-horse-battery", identifier=IP, settings=settings_
        )
        row = conn.execute(
            "SELECT * FROM auth_lockout WHERE identifier = ?", (IP,)
        ).fetchone()
        assert row is None
    finally:
        conn.close()


def test_lockout_window_expires(env: Path, settings_) -> None:
    conn = _conn(env)
    try:
        auth.set_password(conn, "correct-horse-battery")
        past = (datetime.now(UTC) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """
            INSERT INTO auth_lockout (identifier, failed_attempts,
                last_attempt_at, lockout_until)
            VALUES (?, 5, ?, ?)
            """,
            (IP, past, past),
        )
        conn.commit()
        auth.verify_login(
            conn, password="correct-horse-battery", identifier=IP, settings=settings_
        )
    finally:
        conn.close()


def test_verify_with_malformed_hash_returns_invalid_creds(env: Path, settings_) -> None:
    """A corrupt stored hash surfaces as invalid credentials, not a 500."""

    conn = _conn(env)
    try:
        from app.services import settings_store

        settings_store.set_(conn, settings_store.APP_PASSWORD_KEY, "not-a-bcrypt-hash")
        with pytest.raises(auth.InvalidCredentialsError):
            auth.verify_login(conn, password="anything", identifier=IP, settings=settings_)
    finally:
        conn.close()
