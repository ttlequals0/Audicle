from __future__ import annotations

from pathlib import Path

from app.config import get_settings
from app.core import database
from app.core.paths import media_dir
from app.main import create_app
from app.services import episodes
from fastapi.testclient import TestClient


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def _seed_episode(env: Path, *, id_: str, pub_date: str = "2020-01-01T00:00:00Z") -> None:
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id=id_,
            job_id=None,
            original_url=f"https://example.test/{id_}",
            title=id_,
            author="A",
            audio_path=str(media_dir(get_settings()) / f"{id_}.mp3"),
            artwork_path=None,
            transcript_vtt="WEBVTT\n",
            duration_secs=10,
        )
        conn.execute("UPDATE episodes SET pub_date=? WHERE id=?", (pub_date, id_))
        conn.commit()
    finally:
        conn.close()


def test_purge_requires_confirm_query_param(env: Path) -> None:
    _seed_episode(env, id_="ep1")
    with _client(env) as client:
        response = client.post("/api/v1/purge")
    assert response.status_code == 400
    body = response.json()
    assert "confirm" in str(body).lower()


def test_purge_with_confirm_wipes_everything_when_days_zero(env: Path) -> None:
    _seed_episode(env, id_="ep1", pub_date="2099-01-01T00:00:00Z")  # future
    _seed_episode(env, id_="ep2", pub_date="2020-01-01T00:00:00Z")
    with _client(env) as client:
        response = client.post("/api/v1/purge?confirm=true")
    assert response.status_code == 200
    body = response.json()
    assert body["rows_deleted"] == 2
    assert set(body["episode_ids"]) == {"ep1", "ep2"}

    conn = database.connect(database.db_path(env))
    try:
        assert episodes.get_by_id(conn, "ep1") is None
        assert episodes.get_by_id(conn, "ep2") is None
    finally:
        conn.close()


def test_purge_partial_keeps_recent_episodes(env: Path) -> None:
    _seed_episode(env, id_="old", pub_date="2020-01-01T00:00:00Z")
    _seed_episode(env, id_="new", pub_date="2099-01-01T00:00:00Z")
    with _client(env) as client:
        response = client.post("/api/v1/purge?confirm=true&older_than_days=30")
    assert response.status_code == 200
    body = response.json()
    assert body["rows_deleted"] == 1
    assert body["episode_ids"] == ["old"]

    conn = database.connect(database.db_path(env))
    try:
        assert episodes.get_by_id(conn, "old") is None
        assert episodes.get_by_id(conn, "new") is not None
    finally:
        conn.close()


def test_purge_negative_days_rejected_at_validation(env: Path) -> None:
    """FastAPI's ``ge=0`` query-param constraint rejects negative values
    before the handler runs; the custom error handler converts validation
    errors to 400."""

    with _client(env) as client:
        response = client.post("/api/v1/purge?confirm=true&older_than_days=-1")
    assert response.status_code == 400


def test_purge_response_shape(env: Path) -> None:
    _seed_episode(env, id_="ep1")
    with _client(env) as client:
        response = client.post("/api/v1/purge?confirm=true")
    body = response.json()
    assert set(body.keys()) == {
        "older_than_days",
        "rows_deleted",
        "files_removed",
        "episode_ids",
    }
