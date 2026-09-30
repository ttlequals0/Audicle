from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from app.config import get_settings
from app.core import database
from app.main import create_app
from app.services import episodes, file_extraction, jobs
from fastapi.testclient import TestClient


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def _seed_job(env: Path, *, url: str) -> jobs.Job:
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        jobs.create_job(conn, url)
        claimed = jobs.claim_next_queued(conn)
        return claimed
    finally:
        conn.close()


def test_list_jobs_returns_all_when_no_status_filter(env: Path) -> None:
    _seed_job(env, url="https://example.test/a")
    with _client(env) as client:
        response = client.get("/api/v1/jobs")
    assert response.status_code == 200
    assert response.headers["X-Total-Count"] == "1"


def test_list_jobs_filters_by_status(env: Path) -> None:
    job = _seed_job(env, url="https://example.test/b")
    conn = database.connect(database.db_path(env))
    try:
        jobs.mark_failed(conn, job.id, stage="extract", error="boom")
    finally:
        conn.close()
    with _client(env) as client:
        failed = client.get("/api/v1/jobs?status=failed")
        queued = client.get("/api/v1/jobs?status=queued")
    assert failed.headers["X-Total-Count"] == "1"
    assert queued.headers["X-Total-Count"] == "0"


def test_list_jobs_paginates(env: Path) -> None:
    for n in range(5):
        _seed_job(env, url=f"https://example.test/x{n}")
    with _client(env) as client:
        response = client.get("/api/v1/jobs?page=2&per_page=2")
    assert response.headers["X-Total-Count"] == "5"
    assert len(response.json()) == 2


def _set_status(env: Path, job_id: str, status: str) -> None:
    conn = database.connect(database.db_path(env))
    try:
        if status == "done":
            jobs.mark_done(conn, job_id, final_stage="done")
        elif status == "failed":
            jobs.mark_failed(conn, job_id, stage="extract", error="boom")
        elif status == "cancelled":
            jobs.mark_cancelled(conn, job_id)
    finally:
        conn.close()


def test_delete_job_removes_terminal_row(env: Path) -> None:
    job = _seed_job(env, url="https://example.test/del-done")
    _set_status(env, job.id, "done")
    with _client(env) as client:
        response = client.delete(f"/api/v1/jobs/{job.id}")
        listed = client.get("/api/v1/jobs")
    assert response.status_code == 204
    assert listed.headers["X-Total-Count"] == "0"


def test_delete_done_job_preserves_episode_and_clears_provenance(env: Path) -> None:
    job = _seed_job(env, url="https://example.test/del-preserve-episode")
    _set_status(env, job.id, "done")
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id=job.episode_id,
            job_id=job.id,
            original_url=job.url,
            title="Keep me",
            author=None,
            audio_path=None,
            artwork_path=None,
            transcript_vtt=None,
            duration_secs=None,
        )
    finally:
        conn.close()

    with _client(env) as client:
        response = client.delete(f"/api/v1/jobs/{job.id}")
    assert response.status_code == 204
    conn = database.connect(database.db_path(env))
    try:
        episode = episodes.get_by_id(conn, job.episode_id)
        assert episode is not None
        assert episode.job_id is None
        assert jobs.get_job(conn, job.id) is None
    finally:
        conn.close()


def test_requeue_upload_checks_original_and_enqueues_under_upload_lock(
    env: Path, monkeypatch
) -> None:
    filename = "saved.md"
    url = file_extraction.build_source_uri("a" * 64, filename)
    episode_id = jobs.compute_episode_id(url)
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        created = jobs.create_job(conn, url)
        jobs.mark_failed(conn, created.job.id, stage="extract", error="retry")
    finally:
        conn.close()
    source = file_extraction.source_path(get_settings(), episode_id, filename)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"document")

    original_lock = database.upload_staging_lock
    original_create = jobs.create_job
    lock_held = False

    @contextmanager
    def _tracked_lock(data_dir: Path, locked_episode_id: str, *, blocking: bool = True):
        nonlocal lock_held
        with original_lock(data_dir, locked_episode_id, blocking=blocking):
            lock_held = True
            try:
                yield True
            finally:
                lock_held = False

    def _locked_create(*args, **kwargs):
        assert lock_held
        return original_create(*args, **kwargs)

    monkeypatch.setattr(database, "upload_staging_lock", _tracked_lock)
    monkeypatch.setattr(jobs, "create_job", _locked_create)
    with _client(env) as client:
        response = client.post(f"/api/v1/jobs/{created.job.id}/requeue")
    assert response.status_code == 201
    assert response.json()["episode_id"] == episode_id


def test_delete_job_rejects_processing(env: Path) -> None:
    job = _seed_job(env, url="https://example.test/del-processing")
    with _client(env) as client:
        response = client.delete(f"/api/v1/jobs/{job.id}")
    assert response.status_code == 409


def test_delete_job_missing_404(env: Path) -> None:
    database.run_migrations(env)
    with _client(env) as client:
        response = client.delete("/api/v1/jobs/nope")
    assert response.status_code == 404


def test_clear_jobs_failed_scope_keeps_done_and_active(env: Path) -> None:
    done = _seed_job(env, url="https://example.test/clear-done")
    _set_status(env, done.id, "done")
    failed = _seed_job(env, url="https://example.test/clear-failed")
    _set_status(env, failed.id, "failed")
    cancelled = _seed_job(env, url="https://example.test/clear-cancelled")
    _set_status(env, cancelled.id, "cancelled")
    active = _seed_job(env, url="https://example.test/clear-active")  # stays processing
    with _client(env) as client:
        response = client.delete("/api/v1/jobs?scope=failed")
        listed = client.get("/api/v1/jobs")
    assert response.status_code == 200
    assert response.json() == {"removed": 2}
    remaining = {j["id"] for j in listed.json()}
    assert remaining == {done.id, active.id}


def test_clear_jobs_all_scope_keeps_active(env: Path) -> None:
    done = _seed_job(env, url="https://example.test/clearall-done")
    _set_status(env, done.id, "done")
    failed = _seed_job(env, url="https://example.test/clearall-failed")
    _set_status(env, failed.id, "failed")
    active = _seed_job(env, url="https://example.test/clearall-active")
    with _client(env) as client:
        response = client.delete("/api/v1/jobs?scope=all")
        listed = client.get("/api/v1/jobs")
    assert response.json() == {"removed": 2}
    assert {j["id"] for j in listed.json()} == {active.id}


def test_clear_jobs_requires_scope(env: Path) -> None:
    database.run_migrations(env)
    with _client(env) as client:
        response = client.delete("/api/v1/jobs")
    # The app maps validation failures to 400; either way a scopeless clear is rejected.
    assert response.status_code in (400, 422)
