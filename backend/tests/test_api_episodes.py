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


def _seed(env: Path, *, id_: str, with_files: bool = False) -> None:
    database.run_migrations(env)
    media = media_dir(get_settings())
    media.mkdir(parents=True, exist_ok=True)
    audio_path = str(media / f"{id_}.mp3")
    jpg_path = str(media / f"{id_}.jpg")
    if with_files:
        (media / f"{id_}.mp3").write_bytes(b"FAKE")
        (media / f"{id_}.jpg").write_bytes(b"FAKE")
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id=id_,
            job_id=None,
            original_url=f"https://example.test/{id_}",
            title=id_,
            author="A",
            audio_path=audio_path,
            artwork_path=jpg_path if with_files else None,
            transcript_vtt="WEBVTT\n",
            duration_secs=10,
        )
    finally:
        conn.close()


def test_list_episodes_returns_paginated_results_with_total_header(
    env: Path,
) -> None:
    for n in range(3):
        _seed(env, id_=f"ep{n}")
    with _client(env) as client:
        response = client.get("/api/v1/episodes?page=1&per_page=2")
    assert response.status_code == 200
    assert response.headers["X-Total-Count"] == "3"
    assert len(response.json()) == 2


def test_list_episodes_second_page(env: Path) -> None:
    for n in range(5):
        _seed(env, id_=f"ep{n}")
    with _client(env) as client:
        response = client.get("/api/v1/episodes?page=2&per_page=2")
    assert response.headers["X-Total-Count"] == "5"
    assert len(response.json()) == 2


def test_list_episodes_reports_audio_size_bytes(env: Path) -> None:
    _seed(env, id_="sized", with_files=True)  # 4-byte FAKE mp3
    with _client(env) as client:
        response = client.get("/api/v1/episodes")
    assert response.json()[0]["audio_size_bytes"] == 4


def test_list_episodes_audio_size_zero_when_file_missing(env: Path) -> None:
    _seed(env, id_="ghost")  # row present, no file on disk, no stored size
    with _client(env) as client:
        response = client.get("/api/v1/episodes")
    assert response.json()[0]["audio_size_bytes"] == 0


def test_list_episodes_prefers_stored_audio_size_over_stat(env: Path) -> None:
    # A stored audio_size_bytes (0.6.0+) is returned verbatim, even when the file
    # on disk is a different size -- proving no stat() happens for new rows.
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id="sized",
            job_id=None,
            original_url="https://example.test/sized",
            title="t",
            author="a",
            audio_path="/data/media/sized.mp3",
            artwork_path=None,
            transcript_vtt=None,
            duration_secs=10,
            audio_size_bytes=123456,
        )
    finally:
        conn.close()
    with _client(env) as client:
        response = client.get("/api/v1/episodes")
    assert response.json()[0]["audio_size_bytes"] == 123456


def test_list_episodes_has_cleaned_text_flag(env: Path) -> None:
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id="withtext",
            job_id=None,
            original_url="https://example.test/withtext",
            title="t",
            author="a",
            audio_path="/data/media/withtext.mp3",
            artwork_path=None,
            transcript_vtt=None,
            duration_secs=10,
            cleaned_text="cleaned article body",
        )
        episodes.upsert(
            conn,
            id="notext",
            job_id=None,
            original_url="https://example.test/notext",
            title="t",
            author="a",
            audio_path="/data/media/notext.mp3",
            artwork_path=None,
            transcript_vtt=None,
            duration_secs=10,
        )
    finally:
        conn.close()
    with _client(env) as client:
        rows = client.get("/api/v1/episodes").json()
    flags = {r["id"]: r["has_cleaned_text"] for r in rows}
    assert flags == {"withtext": True, "notext": False}


def test_list_episodes_returns_voice_label(env: Path) -> None:
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id="voiced",
            job_id=None,
            original_url="https://example.test/voiced",
            title="t",
            author="a",
            audio_path="/data/media/voiced.mp3",
            artwork_path=None,
            transcript_vtt=None,
            duration_secs=10,
            voice_label="Morgan",
        )
    finally:
        conn.close()
    with _client(env) as client:
        rows = client.get("/api/v1/episodes").json()
    assert rows[0]["voice_label"] == "Morgan"


def test_delete_episode_removes_row_and_files(env: Path) -> None:
    _seed(env, id_="del", with_files=True)
    media = media_dir(get_settings())
    assert (media / "del.mp3").exists()

    with _client(env) as client:
        response = client.delete("/api/v1/episodes/del")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "del"
    assert body["files_removed"] >= 1
    assert not (media / "del.mp3").exists()

    conn = database.connect(database.db_path(env))
    try:
        assert episodes.get_by_id(conn, "del") is None
    finally:
        conn.close()


def test_delete_episode_returns_404_when_missing(env: Path) -> None:
    with _client(env) as client:
        response = client.delete("/api/v1/episodes/unknown")
    assert response.status_code == 404
