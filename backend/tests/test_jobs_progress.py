from __future__ import annotations

from pathlib import Path

from app.core import database
from app.services import jobs


def _job(env: Path) -> tuple[database.sqlite3.Connection, str]:
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    result = jobs.create_job(conn, "https://example.test/article")
    conn.commit()
    return conn, result.job.id


def test_set_progress_records_current_and_total(env: Path) -> None:
    conn, job_id = _job(env)
    try:
        jobs.set_progress(conn, job_id, 57, 162)
        conn.commit()
        job = jobs.get_job(conn, job_id)
        assert job.progress_current == 57
        assert job.progress_total == 162
    finally:
        conn.close()


def test_set_stage_resets_progress(env: Path) -> None:
    conn, job_id = _job(env)
    try:
        jobs.set_progress(conn, job_id, 5, 10)
        jobs.set_stage(conn, job_id, "tts")
        conn.commit()
        job = jobs.get_job(conn, job_id)
        assert job.progress_current is None
        assert job.progress_total is None
    finally:
        conn.close()


def test_claim_sets_started_at(env: Path) -> None:
    conn, job_id = _job(env)
    try:
        # Queued jobs have no start time; claiming stamps it.
        assert jobs.get_job(conn, job_id).started_at is None
        claimed = jobs.claim_next_queued(conn)
        conn.commit()
        assert claimed is not None and claimed.id == job_id
        assert claimed.status == "processing"
        assert claimed.started_at is not None
    finally:
        conn.close()


def test_same_second_jobs_claimed_in_insertion_order(env: Path) -> None:
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        for job_id in ("zz", "aa", "mm"):
            conn.execute(
                "INSERT INTO jobs (id, url, episode_id, status, created_at) "
                "VALUES (?, ?, ?, 'queued', '2026-01-01T00:00:00Z')",
                (job_id, f"https://example.test/{job_id}", job_id),
            )
        assert [jobs.claim_next_queued(conn).id for _ in range(3)] == ["zz", "aa", "mm"]
    finally:
        conn.close()
