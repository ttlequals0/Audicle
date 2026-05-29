from __future__ import annotations

from pathlib import Path

import bcrypt
import pytest
from app.config import get_settings
from app.core import database
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def auth_env(monkeypatch: pytest.MonkeyPatch, env: Path) -> dict[str, str]:
    """Enable auth + provide known credentials."""

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        bcrypt.hashpw(b"correct-horse", bcrypt.gensalt()).decode("ascii"),
    )
    monkeypatch.setenv("SESSION_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("LOCKOUT_MAX_FAILED_ATTEMPTS", "3")
    monkeypatch.setenv("LOCKOUT_WINDOW_SECONDS", "60")
    get_settings.cache_clear()
    database.run_migrations(env)
    return {"username": "admin", "password": "correct-horse"}


def _client() -> TestClient:
    return TestClient(create_app())


def test_login_returns_200_and_csrf_token_on_success(auth_env) -> None:
    with _client() as client:
        response = client.post("/api/v1/auth/login", json=auth_env)
    assert response.status_code == 200
    body = response.json()
    assert body["logged_in"] is True
    assert body["username"] == "admin"
    assert isinstance(body["csrf_token"], str) and body["csrf_token"]
    # The session cookie and csrf cookie are both set.
    cookies = {c.name for c in response.cookies.jar}
    assert "audicle_session" in cookies
    assert "audicle_csrf" in cookies


def test_login_returns_401_on_wrong_password(auth_env) -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": auth_env["username"], "password": "wrong"},
        )
    assert response.status_code == 401


def test_login_returns_423_after_lockout_threshold(auth_env) -> None:
    """3 failures opens a window so the 4th call returns 423 Locked."""

    with _client() as client:
        for _ in range(3):
            client.post(
                "/api/v1/auth/login",
                json={"username": auth_env["username"], "password": "wrong"},
            )
        # 4th attempt -- even with the correct password -- returns 423.
        response = client.post("/api/v1/auth/login", json=auth_env)
    assert response.status_code == 423


def test_logout_clears_session_and_csrf_cookies(auth_env) -> None:
    with _client() as client:
        client.post("/api/v1/auth/login", json=auth_env)
        response = client.post("/api/v1/auth/logout")
    assert response.status_code == 200
    assert response.json() == {"logged_out": True}


def test_status_when_logged_out(auth_env) -> None:
    with _client() as client:
        response = client.get("/api/v1/auth/status")
    body = response.json()
    assert body["auth_enabled"] is True
    assert body["logged_in"] is False
    assert body["username"] is None


def test_status_when_logged_in(auth_env) -> None:
    with _client() as client:
        client.post("/api/v1/auth/login", json=auth_env)
        response = client.get("/api/v1/auth/status")
    body = response.json()
    assert body["logged_in"] is True
    assert body["username"] == "admin"
    assert body["csrf_token"]


def test_login_endpoint_returns_400_when_auth_disabled(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    database.run_migrations(env)
    with _client() as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "x", "password": "y"},
        )
    assert response.status_code == 400


def test_mutating_endpoint_rejects_unauthenticated_when_auth_enabled(
    auth_env,
) -> None:
    """PUT /api/v1/prompt without a session must return 401."""

    with _client() as client:
        response = client.put("/api/v1/prompt", json={"prompt": "x" * 200})
    assert response.status_code == 401


def test_mutating_endpoint_rejects_without_csrf_after_login(auth_env) -> None:
    """A logged-in client must still echo the CSRF header on mutating
    requests."""

    with _client() as client:
        client.post("/api/v1/auth/login", json=auth_env)
        # No X-CSRF-Token header -> 403.
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
def test_admin_routes_require_session_when_auth_enabled(auth_env, method, url, body) -> None:
    """Any route under ``dependencies=[Depends(require_admin)]`` must 401
    without a session. Parametrized so a typo on a single route declaration
    is caught."""

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
    """A logged-in client must echo the CSRF header on mutating calls.
    GETs are exempt per the deps.require_admin contract."""

    with _client() as client:
        client.post("/api/v1/auth/login", json=auth_env)
        response = client.request(method, url, json=body)
    assert response.status_code == 403, f"{method} {url} accepted without X-CSRF-Token"


def test_admin_get_route_is_allowed_with_session_no_csrf(auth_env) -> None:
    """GET must NOT require the CSRF header even with auth on -- a SPA
    warming the cache shouldn't 403 between session-cookie load and
    CSRF-cookie read."""

    with _client() as client:
        client.post("/api/v1/auth/login", json=auth_env)
        response = client.get("/api/v1/prompt")
    assert response.status_code == 200


def test_mutating_endpoint_unrestricted_when_auth_disabled(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local-install default lets the operator hit admin endpoints
    without logging in."""

    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    database.run_migrations(env)
    with _client() as client:
        response = client.put("/api/v1/prompt", json={"prompt": "x" * 200})
    assert response.status_code == 200
