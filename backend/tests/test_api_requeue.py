from __future__ import annotations

from pathlib import Path

import pytest
from app.config import get_settings
from app.core import database
from app.main import create_app
from app.services import file_extraction, jobs, voices
from fastapi.testclient import TestClient


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def _failed_job(env: Path, url: str) -> str:
    with database.connection(env) as conn:
        res = jobs.create_job(conn, url)
        jobs.mark_failed(conn, res.job.id, stage="extract", error="boom")
        conn.commit()
    return res.job.id


def test_requeue_failed_url_job(env: Path) -> None:
    database.run_migrations(env)
    job_id = _failed_job(env, "https://example.test/article")
    with _client(env) as client:
        r = client.post(f"/api/v1/jobs/{job_id}/requeue")
    assert r.status_code == 201
    assert r.json()["status"] == "queued"


def test_requeue_failed_upload_job_with_file(env: Path) -> None:
    database.run_migrations(env)
    uri = file_extraction.build_source_uri("a" * 16, "doc.pdf")
    eid = jobs.compute_episode_id(uri)
    path = file_extraction.source_path(get_settings(), eid, "doc.pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PDF")
    job_id = _failed_job(env, uri)
    with _client(env) as client:
        r = client.post(f"/api/v1/jobs/{job_id}/requeue")
    assert r.status_code == 201


def test_requeue_upload_job_409_when_file_gone(env: Path) -> None:
    database.run_migrations(env)
    uri = file_extraction.build_source_uri("b" * 16, "gone.pdf")
    job_id = _failed_job(env, uri)  # no .source file written
    with _client(env) as client:
        r = client.post(f"/api/v1/jobs/{job_id}/requeue")
    assert r.status_code == 409


def test_requeue_404_when_job_missing(env: Path) -> None:
    with _client(env) as client:
        r = client.post("/api/v1/jobs/nope/requeue")
    assert r.status_code == 404


def test_requeue_rejects_with_400_when_no_voice_loaded(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requeue is a job-creating path, so it carries the same no-voice guard as submit.
    database.run_migrations(env)
    job_id = _failed_job(env, "https://example.test/article")
    empty = tmp_path / "empty_voices"
    empty.mkdir()
    monkeypatch.setattr(voices, "voices_dir", lambda: empty)
    with _client(env) as client:
        r = client.post(f"/api/v1/jobs/{job_id}/requeue")
    assert r.status_code == 400


def test_jobs_list_exposes_source_filename(env: Path) -> None:
    database.run_migrations(env)
    _failed_job(env, file_extraction.build_source_uri("c" * 16, "report.pdf"))
    _failed_job(env, "https://example.test/x")
    with _client(env) as client:
        rows = client.get("/api/v1/jobs").json()
    by_filename = {r["source_filename"] for r in rows}
    assert "report.pdf" in by_filename
    assert None in by_filename  # the url job has no filename
