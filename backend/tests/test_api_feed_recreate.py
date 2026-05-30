from __future__ import annotations

from pathlib import Path

from app.core import database
from app.main import create_app
from app.services import settings_store
from fastapi.testclient import TestClient


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def test_recreate_requires_confirm_query_param(env: Path) -> None:
    with _client(env) as client:
        response = client.post("/api/v1/feed/recreate")
    assert response.status_code == 400
    assert "confirm" in str(response.json()).lower()


def test_recreate_rotates_guid_and_bumps_epoch(env: Path) -> None:
    with _client(env) as client:
        first = client.post("/api/v1/feed/recreate?confirm=true")
        second = client.post("/api/v1/feed/recreate?confirm=true")

    assert first.status_code == 200
    body1 = first.json()
    body2 = second.json()
    assert set(body1.keys()) == {"podcast_guid", "guid_epoch"}
    # Epoch is monotonic; each call rotates to a distinct channel guid.
    assert body1["guid_epoch"] == 1
    assert body2["guid_epoch"] == 2
    assert body1["podcast_guid"] != body2["podcast_guid"]


def test_recreate_persists_epoch_and_guid(env: Path) -> None:
    with _client(env) as client:
        body = client.post("/api/v1/feed/recreate?confirm=true").json()

    conn = database.connect(database.db_path(env))
    try:
        assert settings_store.get_feed_guid_epoch(conn) == 1
        assert settings_store.get(conn, settings_store.PODCAST_GUID_KEY) == body["podcast_guid"]
    finally:
        conn.close()
