from __future__ import annotations

from pathlib import Path

from app.core import database
from app.main import create_app
from fastapi.testclient import TestClient


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def test_get_settings_returns_empty_values_initially(env: Path) -> None:
    with _client(env) as client:
        response = client.get("/api/v1/settings")
    assert response.status_code == 200
    body = response.json()
    assert "RETENTION_DAYS" in body["allowlist"]
    assert body["values"] == {}


def test_put_settings_persists_and_coerces_types(env: Path) -> None:
    with _client(env) as client:
        response = client.put(
            "/api/v1/settings",
            json={
                "RETENTION_DAYS": 30,
                "FEED_TITLE": "My Custom Feed",
                "FEED_EXPLICIT": True,
            },
        )
    assert response.status_code == 200
    values = response.json()["values"]
    assert values["RETENTION_DAYS"] == 30
    assert values["FEED_TITLE"] == "My Custom Feed"
    assert values["FEED_EXPLICIT"] is True


def test_put_settings_rejects_unknown_keys(env: Path) -> None:
    with _client(env) as client:
        response = client.put(
            "/api/v1/settings",
            json={"DATA_DIR": "/tmp/evil"},
        )
    assert response.status_code == 400
    assert "DATA_DIR" in response.json()["error"]


def test_put_then_get_round_trips(env: Path) -> None:
    with _client(env) as client:
        client.put("/api/v1/settings", json={"FEED_AUTHOR": "New Owner"})
        response = client.get("/api/v1/settings")
    assert response.json()["values"]["FEED_AUTHOR"] == "New Owner"
