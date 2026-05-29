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
