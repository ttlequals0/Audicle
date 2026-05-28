from __future__ import annotations

from pathlib import Path

from app.core import database
from app.main import create_app
from fastapi.testclient import TestClient


def _client(data_dir: Path) -> TestClient:
    database.run_migrations(data_dir)
    return TestClient(create_app())


def test_health_live(env: Path) -> None:
    with _client(env) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["version"]


def test_health_ready_ok(env: Path) -> None:
    with _client(env) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["checks"]["db"] == "ok"
    assert body["components"]["app"]
    assert body["components"]["python"]


def test_health_alias_returns_same_shape(env: Path) -> None:
    with _client(env) as client:
        ready = client.get("/health/ready").json()
        alias = client.get("/health").json()
    assert ready["checks"] == alias["checks"]
    assert ready["components"] == alias["components"]


def test_health_ready_503_when_db_unreachable(env: Path, monkeypatch) -> None:
    # Let the lifespan migration succeed against the real DB, then break only
    # the health endpoint's connect.
    database.run_migrations(env)

    def _broken_connect(*_args, **_kwargs):
        raise RuntimeError("simulated db failure")

    with TestClient(create_app()) as client:
        monkeypatch.setattr(database, "connect", _broken_connect)
        response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert "error" in body["checks"]["db"]
