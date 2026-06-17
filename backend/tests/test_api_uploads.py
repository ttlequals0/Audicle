from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.config import get_settings
from app.core import database
from app.core.paths import media_dir
from app.main import create_app
from app.services import episodes, feed, file_extraction, jobs, runtime_settings, voices
from app.services.episodes import Episode
from fastapi.testclient import TestClient


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def _seed_upload_episode(env: Path, *, filename: str, write_source: bool = True) -> str:
    """Insert an upload episode (and optionally its stored original) and return its id."""

    database.run_migrations(env)
    media = media_dir(get_settings())
    media.mkdir(parents=True, exist_ok=True)
    uri = file_extraction.build_source_uri("a" * 16, filename)
    episode_id = jobs.compute_episode_id(uri)
    (media / f"{episode_id}.mp3").write_bytes(b"FAKE")
    if write_source:
        file_extraction.source_path(get_settings(), episode_id, filename).write_bytes(b"DOC")
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id=episode_id,
            job_id=None,
            original_url=uri,
            title=Path(filename).stem,
            author="A",
            audio_path=str(media / f"{episode_id}.mp3"),
            artwork_path=None,
            transcript_vtt="WEBVTT\n",
            duration_secs=10,
            source_type="upload",
            source_filename=filename,
        )
    finally:
        conn.close()
    return episode_id


# --- POST /upload -------------------------------------------------------------


def test_upload_md_creates_job_and_stores_original(env: Path) -> None:
    content = b"# Hi\n\nsome body text"
    with _client(env) as client:
        r = client.post("/api/v1/upload", files={"file": ("notes.md", content, "text/markdown")})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "queued"
    episode_id = body["episode_id"]
    assert (media_dir(get_settings()) / f"{episode_id}.source.md").read_bytes() == content
    conn = database.connect(database.db_path(env))
    try:
        job = jobs.get_job(conn, body["job_id"])
        assert job is not None
        assert job.url.startswith("upload://")
        assert job.episode_id == episode_id
    finally:
        conn.close()


def test_upload_rejects_with_400_when_no_voice_loaded(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mirrors the submit guard: no loaded voice -> 400 before any file work.
    from app.services import voices

    empty = tmp_path / "empty_voices"
    empty.mkdir()
    monkeypatch.setattr(voices, "voices_dir", lambda: empty)
    with _client(env) as client:
        r = client.post("/api/v1/upload", files={"file": ("notes.md", b"# Hi", "text/markdown")})
    assert r.status_code == 400


def test_upload_rejects_unsupported_extension(env: Path) -> None:
    with _client(env) as client:
        r = client.post(
            "/api/v1/upload",
            files={"file": ("malware.exe", b"x" * 20, "application/octet-stream")},
        )
    assert r.status_code == 400


def test_upload_rejects_empty_file(env: Path) -> None:
    with _client(env) as client:
        r = client.post("/api/v1/upload", files={"file": ("empty.md", b"", "text/markdown")})
    assert r.status_code == 400


def test_upload_rejects_oversize(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1 MB cap; a >1 MB payload is rejected (the cap is megabytes now).
    monkeypatch.setenv("UPLOAD_MAX_MB", "1")
    get_settings.cache_clear()
    with _client(env) as client:
        r = client.post(
            "/api/v1/upload", files={"file": ("big.md", b"x" * 1_100_000, "text/markdown")}
        )
    assert r.status_code == 400


def test_upload_respects_runtime_tuned_max_mb(env: Path) -> None:
    # A DB override (the operator-tunable path, not just env) drives the cap.
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        runtime_settings.set_value(conn, "UPLOAD_MAX_MB", 1)
    finally:
        conn.close()
    with _client(env) as client:
        r = client.post(
            "/api/v1/upload", files={"file": ("big.md", b"x" * 1_100_000, "text/markdown")}
        )
    assert r.status_code == 400


def test_upload_duplicate_returns_409(env: Path) -> None:
    payload = {"file": ("a.md", b"identical bytes for both", "text/markdown")}
    with _client(env) as client:
        first = client.post("/api/v1/upload", files=payload)
        assert first.status_code == 201
        second = client.post(
            "/api/v1/upload", files={"file": ("a.md", b"identical bytes for both", "text/markdown")}
        )
    assert second.status_code == 409


def test_upload_episode_list_exposes_source_fields(env: Path) -> None:
    _seed_upload_episode(env, filename="report.pdf")
    with _client(env) as client:
        rows = client.get("/api/v1/episodes").json()
    assert rows[0]["source_type"] == "upload"
    assert rows[0]["source_filename"] == "report.pdf"


# --- POST /upload/{id}/reprocess ---------------------------------------------


def test_reprocess_upload_reenqueues_from_stored_file(env: Path) -> None:
    episode_id = _seed_upload_episode(env, filename="report.pdf")
    with _client(env) as client:
        r = client.post(f"/api/v1/upload/{episode_id}/reprocess")
    assert r.status_code == 201
    body = r.json()
    assert body["episode_id"] == episode_id
    assert body["replaced_previous"] is True


def test_reprocess_upload_rejects_with_400_when_no_voice_loaded(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reprocess is a job-creating path, so it carries the same no-voice guard as upload.
    episode_id = _seed_upload_episode(env, filename="report.pdf")
    empty = tmp_path / "empty_voices"
    empty.mkdir()
    monkeypatch.setattr(voices, "voices_dir", lambda: empty)
    with _client(env) as client:
        r = client.post(f"/api/v1/upload/{episode_id}/reprocess")
    assert r.status_code == 400


def test_reprocess_rejects_url_episode(env: Path) -> None:
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id="urlep",
            job_id=None,
            original_url="https://example.test/urlep",
            title="t",
            author="a",
            audio_path="/data/media/urlep.mp3",
            artwork_path=None,
            transcript_vtt=None,
            duration_secs=10,
        )
    finally:
        conn.close()
    with _client(env) as client:
        r = client.post("/api/v1/upload/urlep/reprocess")
    assert r.status_code == 400


def test_reprocess_404_when_episode_missing(env: Path) -> None:
    with _client(env) as client:
        r = client.post("/api/v1/upload/ghost/reprocess")
    assert r.status_code == 404


def test_reprocess_409_when_stored_file_gone(env: Path) -> None:
    episode_id = _seed_upload_episode(env, filename="report.pdf", write_source=False)
    with _client(env) as client:
        r = client.post(f"/api/v1/upload/{episode_id}/reprocess")
    assert r.status_code == 409


# --- RSS rendering ------------------------------------------------------------


def test_feed_render_upload_episode_has_no_synthetic_link(env: Path) -> None:
    ep = Episode(
        id="up123",
        job_id="job1",
        title="My Report",
        author="A",
        original_url="upload://abc123/My%20Report.pdf",
        audio_path="/data/media/up123.mp3",
        artwork_path=None,
        transcript_vtt="WEBVTT\n",
        duration_secs=60,
        pub_date="2026-05-28T18:00:00Z",
        created_at="2026-05-28T18:00:00Z",
        updated_at="2026-05-28T18:00:00Z",
        source_type="upload",
        source_filename="My Report.pdf",
    )
    body = feed.render(
        [ep],
        settings=get_settings(),
        podcast_guid="11111111-2222-3333-4444-555555555555",
        last_build=datetime(2026, 5, 28, 18, 0, 0, tzinfo=UTC),
    )
    assert b"upload://" not in body
    assert b"My Report.pdf" in body
