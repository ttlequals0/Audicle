from __future__ import annotations

from pathlib import Path

from app.core import database
from app.main import create_app
from app.services import jobs
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
