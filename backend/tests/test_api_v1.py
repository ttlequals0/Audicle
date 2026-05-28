from __future__ import annotations

from pathlib import Path

import pytest
from app.core import database
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def test_submit_returns_201_with_job_id_and_episode_id(client: TestClient) -> None:
    with client:
        response = client.post("/api/v1/submit", json={"url": "https://example.test/article"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert len(body["episode_id"]) == 12
    assert body["job_id"]
    assert body["replaced_previous"] is False


def test_submit_rejects_invalid_url_with_400(client: TestClient) -> None:
    with client:
        response = client.post("/api/v1/submit", json={"url": "not-a-url"})
    assert response.status_code == 400
    body = response.json()
    assert body["status"] == 400
    assert body["error"] == "Validation failed"
    assert "details" in body


def test_submit_rejects_inflight_duplicate_with_409(client: TestClient) -> None:
    with client:
        first = client.post("/api/v1/submit", json={"url": "https://example.test/dup"})
        assert first.status_code == 201
        second = client.post("/api/v1/submit", json={"url": "https://example.test/dup"})
    assert second.status_code == 409
    body = second.json()
    assert body["status"] == 409
    assert body["error"] == "Episode already exists"
    assert body["details"]["episode_id"] == first.json()["episode_id"]


def test_submit_reprocess_allows_resubmit_after_episode_landed(
    client: TestClient, env: Path
) -> None:
    with client:
        first = client.post("/api/v1/submit", json={"url": "https://example.test/reproc"})
        first_episode_id = first.json()["episode_id"]
        # Simulate Phase 7+ creating an episode + finishing the job.
        conn = database.connect(database.db_path(env))
        try:
            conn.execute(
                "UPDATE jobs SET status='done', stage='done' WHERE id = ?",
                (first.json()["job_id"],),
            )
            conn.execute(
                "INSERT INTO episodes (id, original_url) VALUES (?, ?)",
                (first_episode_id, "https://example.test/reproc"),
            )
        finally:
            conn.close()

        # Default reprocess=false -> 409
        dup = client.post("/api/v1/submit", json={"url": "https://example.test/reproc"})
        assert dup.status_code == 409

        # reprocess=true -> 201 + replaced_previous=True
        again = client.post(
            "/api/v1/submit",
            json={"url": "https://example.test/reproc", "reprocess": True},
        )
    assert again.status_code == 201
    body = again.json()
    assert body["episode_id"] == first_episode_id
    assert body["replaced_previous"] is True


def test_status_returns_200_for_existing_job(client: TestClient) -> None:
    with client:
        submit = client.post("/api/v1/submit", json={"url": "https://example.test/status"})
        job_id = submit.json()["job_id"]
        status = client.get(f"/api/v1/status/{job_id}")
    assert status.status_code == 200
    body = status.json()
    assert body["job_id"] == job_id
    assert body["status"] == "queued"
    assert body["url"] == "https://example.test/status"
    assert body["stage"] is None
    assert body["error"] is None


def test_status_returns_404_for_missing_job(client: TestClient) -> None:
    with client:
        response = client.get("/api/v1/status/no-such-job")
    assert response.status_code == 404
    body = response.json()
    assert body["status"] == 404
    assert body["error"] == "Job not found"


def test_submit_rejects_unknown_fields_with_400(client: TestClient) -> None:
    with client:
        response = client.post(
            "/api/v1/submit",
            json={"url": "https://example.test/typo", "reprcess": True},
        )
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "Validation failed"


def test_submit_preserves_raw_url_through_to_status(client: TestClient) -> None:
    """AnyHttpUrl normalizes (host lowercasing, trailing slash); the submit
    handler intentionally stores the user's exact string so episode_id and the
    returned URL match what the user sent."""

    with client:
        raw = "https://Example.test/Article?id=42"
        submit = client.post("/api/v1/submit", json={"url": raw})
        assert submit.status_code == 201
        job_id = submit.json()["job_id"]
        status = client.get(f"/api/v1/status/{job_id}")
    assert status.json()["url"] == raw


def test_reprocess_still_409s_when_a_concurrent_inflight_job_exists(
    client: TestClient, env: Path
) -> None:
    """The in_flight guard runs BEFORE the reprocess wipe; even with
    reprocess=True, a queued/processing job for the same URL must reject."""

    with client:
        first = client.post(
            "/api/v1/submit",
            json={"url": "https://example.test/race"},
        )
        assert first.status_code == 201

        again = client.post(
            "/api/v1/submit",
            json={"url": "https://example.test/race", "reprocess": True},
        )
    assert again.status_code == 409
    assert "already queued" in again.json()["details"]["reason"]


def test_status_returns_failed_job_with_stage_and_error(client: TestClient, env: Path) -> None:
    with client:
        submit = client.post("/api/v1/submit", json={"url": "https://example.test/failed"})
        job_id = submit.json()["job_id"]
        # Simulate the worker pipeline marking the job failed.
        conn = database.connect(database.db_path(env))
        try:
            conn.execute(
                "UPDATE jobs SET status='failed', stage='extract', "
                "error='Firecrawl said no' WHERE id = ?",
                (job_id,),
            )
        finally:
            conn.close()
        response = client.get(f"/api/v1/status/{job_id}")
    body = response.json()
    assert body["status"] == "failed"
    assert body["stage"] == "extract"
    assert body["error"] == "Firecrawl said no"


def test_unhandled_exception_returns_500_envelope_without_leaking_details(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 'never leak details on 500' contract gets a dedicated test by
    forcing an unhandled exception inside the status endpoint."""

    database.run_migrations(env)
    # TestClient re-raises server errors by default; flip the flag so the
    # envelope returned by the catch-all handler is observable.
    raw_client = TestClient(create_app(), raise_server_exceptions=False)
    with raw_client as client:
        submit = client.post("/api/v1/submit", json={"url": "https://example.test/boom"})
        job_id = submit.json()["job_id"]

        from app.services import jobs

        def _boom(*_args, **_kwargs):
            raise RuntimeError("super-secret-internal-detail")

        monkeypatch.setattr(jobs, "get_job", _boom)
        response = client.get(f"/api/v1/status/{job_id}")
    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "Internal server error"
    assert "super-secret-internal-detail" not in str(body)
    assert "details" not in body or body.get("details") is None
