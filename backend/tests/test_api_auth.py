from __future__ import annotations

from pathlib import Path

import pytest
from app.config import get_settings
from app.core import database
from app.main import create_app
from app.services import auth
from fastapi.testclient import TestClient

PASSWORD = "correct-horse"


def _set_password(env: Path, plaintext: str) -> None:
    conn = database.connect(database.db_path(env))
    try:
        auth.set_password(conn, plaintext)
    finally:
        conn.close()


@pytest.fixture
def auth_env(monkeypatch: pytest.MonkeyPatch, env: Path) -> dict[str, str]:
    """Password-protected mode: a bcrypt hash stored in the settings table."""

    monkeypatch.setenv("LOCKOUT_MAX_FAILED_ATTEMPTS", "3")
    monkeypatch.setenv("LOCKOUT_WINDOW_SECONDS", "60")
    get_settings.cache_clear()
    database.run_migrations(env)
    _set_password(env, PASSWORD)
    return {"password": PASSWORD}


@pytest.fixture
def open_env(env: Path) -> Path:
    """Convenience mode: no password set, migrations applied."""

    database.run_migrations(env)
    return env


def _client() -> TestClient:
    return TestClient(create_app())


def test_login_returns_200_and_csrf_token_on_success(auth_env) -> None:
    with _client() as client:
        response = client.post("/api/v1/auth/login", json=auth_env)
    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True
    assert body["password_set"] is True
    assert isinstance(body["csrf_token"], str) and body["csrf_token"]
    cookies = {c.name for c in response.cookies.jar}
    assert "audicle_session" in cookies
    assert "audicle_csrf" in cookies


def test_login_returns_401_on_wrong_password(auth_env) -> None:
    with _client() as client:
        response = client.post("/api/v1/auth/login", json={"password": "wrong"})
    assert response.status_code == 401


def test_login_returns_423_after_lockout_threshold(auth_env) -> None:
    with _client() as client:
        for _ in range(3):
            client.post("/api/v1/auth/login", json={"password": "wrong"})
        response = client.post("/api/v1/auth/login", json=auth_env)
    assert response.status_code == 423


def test_logout_clears_session(auth_env) -> None:
    with _client() as client:
        client.post("/api/v1/auth/login", json=auth_env)
        response = client.post("/api/v1/auth/logout")
    assert response.status_code == 200
    assert response.json() == {"authenticated": False}


def test_status_when_logged_out(auth_env) -> None:
    with _client() as client:
        response = client.get("/api/v1/auth/status")
    body = response.json()
    assert body["password_set"] is True
    assert body["authenticated"] is False


def test_status_when_logged_in(auth_env) -> None:
    with _client() as client:
        client.post("/api/v1/auth/login", json=auth_env)
        response = client.get("/api/v1/auth/status")
    body = response.json()
    assert body["password_set"] is True
    assert body["authenticated"] is True
    assert body["csrf_token"]


def test_status_convenience_mode_reports_authenticated(open_env) -> None:
    """No password set -> open convenience mode, authenticated reported true."""

    with _client() as client:
        response = client.get("/api/v1/auth/status")
    body = response.json()
    assert body["password_set"] is False
    assert body["authenticated"] is True


def test_login_returns_400_when_no_password_set(open_env) -> None:
    with _client() as client:
        response = client.post("/api/v1/auth/login", json={"password": "y"})
    assert response.status_code == 400


def test_set_password_first_time_in_convenience_mode(open_env: Path) -> None:
    """PUT /auth/password with no current_password sets the first password and
    logs the session in."""

    with _client() as client:
        response = client.put("/api/v1/auth/password", json={"new_password": "s3cret-pass"})
        assert response.status_code == 200
        body = response.json()
        assert body["password_set"] is True
        assert body["authenticated"] is True
        # The password now gates the API.
        status = client.get("/api/v1/auth/status").json()
        assert status["password_set"] is True


def test_set_password_rejects_short(open_env) -> None:
    with _client() as client:
        response = client.put("/api/v1/auth/password", json={"new_password": "short"})
    assert response.status_code == 400


def test_change_password_requires_current(auth_env) -> None:
    with _client() as client:
        login = client.post("/api/v1/auth/login", json=auth_env)
        token = login.json()["csrf_token"]
        # Missing current_password -> 400.
        missing = client.put(
            "/api/v1/auth/password",
            json={"new_password": "another-secret"},
            headers={"X-CSRF-Token": token},
        )
        assert missing.status_code == 400
        # Wrong current_password -> 401.
        wrong = client.put(
            "/api/v1/auth/password",
            json={"current_password": "nope", "new_password": "another-secret"},
            headers={"X-CSRF-Token": token},
        )
        assert wrong.status_code == 401
        # Correct current_password -> 200.
        ok = client.put(
            "/api/v1/auth/password",
            json={"current_password": PASSWORD, "new_password": "another-secret"},
            headers={"X-CSRF-Token": token},
        )
        assert ok.status_code == 200


def test_clear_password_reverts_to_convenience_mode(auth_env) -> None:
    with _client() as client:
        login = client.post("/api/v1/auth/login", json=auth_env)
        token = login.json()["csrf_token"]
        response = client.put(
            "/api/v1/auth/password",
            json={"current_password": PASSWORD, "new_password": ""},
            headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 200
        assert response.json()["password_set"] is False
        status = client.get("/api/v1/auth/status").json()
        assert status["password_set"] is False
        assert status["authenticated"] is True


def test_mutating_endpoint_rejects_unauthenticated_when_password_set(auth_env) -> None:
    with _client() as client:
        response = client.put("/api/v1/prompt", json={"prompt": "x" * 200})
    assert response.status_code == 401


def test_mutating_endpoint_rejects_without_csrf_after_login(auth_env) -> None:
    with _client() as client:
        client.post("/api/v1/auth/login", json=auth_env)
        response = client.put("/api/v1/prompt", json={"prompt": "x" * 200})
    assert response.status_code == 403


def test_mutating_endpoint_succeeds_with_session_and_csrf(auth_env) -> None:
    with _client() as client:
        login = client.post("/api/v1/auth/login", json=auth_env)
        token = login.json()["csrf_token"]
        response = client.put(
            "/api/v1/prompt",
            json={"prompt": "x" * 200},
            headers={"X-CSRF-Token": token},
        )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "method,url,body",
    [
        ("GET", "/api/v1/prompt", None),
        ("GET", "/api/v1/corrections", None),
        ("GET", "/api/v1/settings", None),
        ("GET", "/api/v1/episodes", None),
        ("GET", "/api/v1/jobs", None),
        ("PUT", "/api/v1/settings", {"FEED_TITLE": "x"}),
        ("PUT", "/api/v1/prompt", {"prompt": "x" * 200}),
        ("DELETE", "/api/v1/episodes/abc123", None),
        ("POST", "/api/v1/submit", {"url": "https://example.test/x"}),
        ("POST", "/api/v1/purge?confirm=true", None),
    ],
)
def test_admin_routes_require_session_when_password_set(auth_env, method, url, body) -> None:
    with _client() as client:
        response = client.request(method, url, json=body)
    assert response.status_code == 401, f"{method} {url} leaked without a session"


@pytest.mark.parametrize(
    "method,url,body",
    [
        ("PUT", "/api/v1/settings", {"FEED_TITLE": "x"}),
        ("PUT", "/api/v1/prompt", {"prompt": "x" * 200}),
        ("DELETE", "/api/v1/episodes/abc123", None),
        ("POST", "/api/v1/submit", {"url": "https://example.test/x"}),
        ("POST", "/api/v1/purge?confirm=true", None),
    ],
)
def test_admin_mutating_routes_require_csrf_after_login(auth_env, method, url, body) -> None:
    with _client() as client:
        client.post("/api/v1/auth/login", json=auth_env)
        response = client.request(method, url, json=body)
    assert response.status_code == 403, f"{method} {url} accepted without X-CSRF-Token"


def test_admin_get_route_is_allowed_with_session_no_csrf(auth_env) -> None:
    with _client() as client:
        client.post("/api/v1/auth/login", json=auth_env)
        response = client.get("/api/v1/prompt")
    assert response.status_code == 200


def test_mutating_endpoint_unrestricted_in_convenience_mode(open_env) -> None:
    """No password set: the operator can hit admin endpoints without login."""

    with _client() as client:
        response = client.put("/api/v1/prompt", json={"prompt": "x" * 200})
    assert response.status_code == 200
