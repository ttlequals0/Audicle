from __future__ import annotations

from pathlib import Path

from app.core import database
from app.main import create_app
from app.services import jobs
from fastapi.testclient import TestClient


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def _queued(env: Path, url: str) -> str:
    with database.connection(env) as conn:
        res = jobs.create_job(conn, url)
        conn.commit()
    return res.job.id


def test_cancel_queued_job(env: Path) -> None:
    database.run_migrations(env)
    job_id = _queued(env, "https://example.test/a")
    with _client(env) as client:
        r = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert r.status_code == 204
    with database.connection(env) as conn:
        assert jobs.get_job(conn, job_id).status == "cancelled"


def test_cancel_processing_job(env: Path) -> None:
    database.run_migrations(env)
    job_id = _queued(env, "https://example.test/b")
    with database.connection(env) as conn:
        jobs.claim_next_queued(conn)  # queued -> processing
        conn.commit()
    with _client(env) as client:
        r = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert r.status_code == 204
    with database.connection(env) as conn:
        assert jobs.get_job(conn, job_id).status == "cancelled"


def test_cancel_terminal_job_conflicts(env: Path) -> None:
    database.run_migrations(env)
    job_id = _queued(env, "https://example.test/c")
    with database.connection(env) as conn:
        jobs.mark_done(conn, job_id, final_stage="finalize")
        conn.commit()
    with _client(env) as client:
        r = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert r.status_code == 409
    with database.connection(env) as conn:
        assert jobs.get_job(conn, job_id).status == "done"  # unchanged


def test_cancel_missing_job_404(env: Path) -> None:
    with _client(env) as client:
        r = client.post("/api/v1/jobs/does-not-exist/cancel")
    assert r.status_code == 404


def test_mark_done_does_not_overwrite_cancelled(env: Path) -> None:
    # A cancel that lands while the worker finishes must win: mark_done refuses to
    # overwrite a 'cancelled' row, so the episode isn't silently published.
    database.run_migrations(env)
    job_id = _queued(env, "https://example.test/d")
    with database.connection(env) as conn:
        jobs.mark_cancelled(conn, job_id)
        jobs.mark_done(conn, job_id, final_stage="finalize")
        conn.commit()
        assert jobs.get_job(conn, job_id).status == "cancelled"


def test_mark_cancelled_does_not_reopen_terminal_job(env: Path) -> None:
    # A cancel that arrives after the job already finished is a no-op.
    database.run_migrations(env)
    job_id = _queued(env, "https://example.test/e")
    with database.connection(env) as conn:
        jobs.mark_done(conn, job_id, final_stage="finalize")
        jobs.mark_cancelled(conn, job_id)
        conn.commit()
        assert jobs.get_job(conn, job_id).status == "done"
