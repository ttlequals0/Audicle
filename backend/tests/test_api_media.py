from __future__ import annotations

from pathlib import Path

from app.core import database
from app.core.paths import media_dir
from app.main import create_app
from app.services import episodes
from fastapi.testclient import TestClient


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def _seed_episode(
    env: Path, *, id_: str, transcript_vtt: str | None, cleaned_text: str | None = None
) -> Path:
    database.run_migrations(env)
    from app.config import get_settings

    media = media_dir(get_settings())
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id=id_,
            job_id=None,
            original_url="https://example.test/a",
            title="An Article",
            author="Author",
            audio_path=str(media / f"{id_}.mp3"),
            artwork_path=str(media / f"{id_}.jpg"),
            transcript_vtt=transcript_vtt,
            duration_secs=10,
            cleaned_text=cleaned_text,
        )
    finally:
        conn.close()
    return media


def test_get_mp3_serves_disk_file_with_audio_content_type(env: Path) -> None:
    media = _seed_episode(env, id_="abc", transcript_vtt="WEBVTT\n")
    media.mkdir(parents=True, exist_ok=True)
    (media / "abc.mp3").write_bytes(b"FAKE_MP3")
    with _client(env) as client:
        response = client.get("/media/abc.mp3")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b"FAKE_MP3"


def test_get_jpg_serves_disk_file_with_image_content_type(env: Path) -> None:
    media = _seed_episode(env, id_="abc", transcript_vtt=None)
    media.mkdir(parents=True, exist_ok=True)
    (media / "abc.jpg").write_bytes(b"FAKE_JPG")
    with _client(env) as client:
        response = client.get("/media/abc.jpg")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == b"FAKE_JPG"


def test_versioned_jpg_path_serves_exact_generation(env: Path) -> None:
    media = _seed_episode(env, id_="abc", transcript_vtt=None)
    media.mkdir(parents=True, exist_ok=True)
    old = media / "abc.old.jpg"
    old.write_bytes(b"OLD_JPG")
    conn = database.connect(database.db_path(env))
    try:
        episode = episodes.get_by_id(conn, "abc")
        assert episode is not None
        token = "a" * 32
        episodes.publish_generation(
            conn,
            "abc",
            episode.generation_token,
            {"artwork_path": str(old)},
            token=token,
        )
    finally:
        conn.close()
    with _client(env) as client:
        response = client.get(f"/media/abc-v{token}.jpg")
    assert response.status_code == 200
    assert response.content == b"OLD_JPG"


def test_old_audio_generation_remains_range_readable_after_publish(env: Path) -> None:
    media = _seed_episode(env, id_="abc", transcript_vtt="OLD VTT")
    media.mkdir(parents=True, exist_ok=True)
    old_audio = media / "abc.mp3"
    old_audio.write_bytes(b"OLD_AUDIO")
    new_audio = media / "abc.new.mp3"
    new_audio.write_bytes(b"NEW_AUDIO")
    conn = database.connect(database.db_path(env))
    try:
        before = episodes.get_by_id(conn, "abc")
        assert before is not None and before.generation_token
        old_token = before.generation_token
        new_token = "b" * 32
        episodes.publish_generation(
            conn,
            "abc",
            old_token,
            {"audio_path": str(new_audio), "audio_size_bytes": len(b"NEW_AUDIO")},
            token=new_token,
        )
    finally:
        conn.close()
    with _client(env) as client:
        current = client.get("/media/abc.mp3")
        old = client.get(f"/media/abc.mp3?v={old_token}", headers={"Range": "bytes=0-2"})
    assert current.content == b"NEW_AUDIO"
    assert old.status_code == 206
    assert old.content == b"OLD"


def test_head_jpg_returns_200_with_headers_and_no_body(env: Path) -> None:
    # Artwork fetchers issue HEAD before GET; the route must answer it, not 405.
    media = _seed_episode(env, id_="abc", transcript_vtt=None)
    media.mkdir(parents=True, exist_ok=True)
    (media / "abc.jpg").write_bytes(b"FAKE_JPG")
    with _client(env) as client:
        response = client.head("/media/abc.jpg")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == b""


def test_head_mp3_returns_200_with_headers_and_no_body(env: Path) -> None:
    media = _seed_episode(env, id_="abc", transcript_vtt="WEBVTT\n")
    media.mkdir(parents=True, exist_ok=True)
    (media / "abc.mp3").write_bytes(b"FAKE_MP3")
    with _client(env) as client:
        response = client.head("/media/abc.mp3")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b""


def test_get_vtt_serves_transcript_from_db_with_cache_header(env: Path) -> None:
    _seed_episode(env, id_="abc", transcript_vtt="WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.000\nhi\n")
    with _client(env) as client:
        response = client.get("/media/abc.vtt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/vtt")
    assert "max-age=86400" in response.headers["cache-control"]
    assert response.text.startswith("WEBVTT")


def test_get_mp3_returns_404_when_file_missing(env: Path) -> None:
    _seed_episode(env, id_="abc", transcript_vtt=None)
    with _client(env) as client:
        response = client.get("/media/abc.mp3")
    assert response.status_code == 404


def test_get_jpg_returns_404_when_file_missing(env: Path) -> None:
    _seed_episode(env, id_="abc", transcript_vtt=None)
    with _client(env) as client:
        response = client.get("/media/abc.jpg")
    assert response.status_code == 404


def test_get_cleaned_text_serves_from_db(env: Path) -> None:
    _seed_episode(env, id_="abc", transcript_vtt=None, cleaned_text="The cleaned article.")
    with _client(env) as client:
        response = client.get("/media/abc.txt")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.content == b"The cleaned article."


def test_get_cleaned_text_returns_404_when_null(env: Path) -> None:
    _seed_episode(env, id_="abc", transcript_vtt="WEBVTT\n", cleaned_text=None)
    with _client(env) as client:
        response = client.get("/media/abc.txt")
    assert response.status_code == 404


def test_get_vtt_returns_404_when_episode_missing(env: Path) -> None:
    with _client(env) as client:
        response = client.get("/media/unknown.vtt")
    assert response.status_code == 404


def test_get_vtt_returns_404_when_transcript_null(env: Path) -> None:
    _seed_episode(env, id_="abc", transcript_vtt=None)
    with _client(env) as client:
        response = client.get("/media/abc.vtt")
    assert response.status_code == 404


def test_media_routes_reject_path_traversal(env: Path) -> None:
    """The episode_id regex blocks `..` so a client can't escape media_dir."""

    with _client(env) as client:
        response = client.get("/media/..%2f..%2fetc%2fpasswd.mp3")
    # FastAPI URL-decodes the path, so the literal `..` reaches the route and
    # the regex check returns 404 (not 200, not 500).
    assert response.status_code == 404


def test_media_routes_reject_dot_prefixed_id(env: Path) -> None:
    """An id that DOES match the route pattern but contains characters
    outside ``[A-Za-z0-9_-]`` must be rejected by the in-handler regex.
    The lowercase ``not found`` body proves ``_validate_episode_id`` fired
    rather than Starlette's router 404'ing on a non-matching path."""

    with _client(env) as client:
        response = client.get("/media/.hidden.mp3")
    assert response.status_code == 404
    # The app's custom error handler renders ``{"error": <detail>, "status":
    # <code>}`` (lowercase "not found"); Starlette's default router 404
    # would produce ``"Not Found"`` (capital N), so the lowercase body
    # proves ``_validate_episode_id`` is what rejected the request.
    assert response.json() == {"error": "not found", "status": 404}


def test_get_chapters_json_serves_stored_document(env: Path) -> None:
    _seed_episode(env, id_="abc", transcript_vtt=None)
    conn = database.connect(database.db_path(env))
    try:
        episode = episodes.get_by_id(conn, "abc")
        assert episode is not None
        episodes.publish_generation(
            conn,
            "abc",
            episode.generation_token,
            {"chapters_json": '{"version": "1.2.0", "chapters": []}'},
        )
    finally:
        conn.close()
    with _client(env) as client:
        response = client.get("/media/abc.chapters.json")
    assert response.status_code == 200
    assert response.json()["version"] == "1.2.0"


def test_get_chapters_json_404_when_absent(env: Path) -> None:
    _seed_episode(env, id_="abc", transcript_vtt=None)
    with _client(env) as client:
        response = client.get("/media/abc.chapters.json")
    assert response.status_code == 404
