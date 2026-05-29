"""Endpoint coverage for /api/v1/prompt and /api/v1/corrections."""

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


# --- /api/v1/prompt --------------------------------------------------------


def test_get_prompt_returns_current_file_contents(client: TestClient) -> None:
    with client:
        response = client.get("/api/v1/prompt")
    assert response.status_code == 200
    body = response.json()
    assert "prompt" in body
    assert isinstance(body["prompt"], str)
    assert len(body["prompt"]) > 0


def test_put_prompt_persists_for_next_get(client: TestClient) -> None:
    with client:
        new_text = "Phase 3 test prompt body"
        put = client.put("/api/v1/prompt", json={"prompt": new_text})
        assert put.status_code == 200
        assert put.json()["prompt"] == new_text
        after = client.get("/api/v1/prompt").json()["prompt"]
    assert after == new_text


def test_put_prompt_rejects_extra_fields(client: TestClient) -> None:
    with client:
        response = client.put("/api/v1/prompt", json={"prompt": "ok", "extra": "nope"})
    assert response.status_code == 400
    assert response.json()["error"] == "Validation failed"


def test_put_prompt_413_on_oversize(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_PROMPT_LENGTH_BYTES", "32")
    from app.config import get_settings

    get_settings.cache_clear()
    with client:
        response = client.put("/api/v1/prompt", json={"prompt": "x" * 100})
    assert response.status_code == 413
    body = response.json()
    assert body["error"] == "Prompt too large"
    assert body["details"]["max_bytes"] == 32


# --- /api/v1/corrections ---------------------------------------------------


def test_get_corrections_returns_dictionary(client: TestClient) -> None:
    with client:
        response = client.get("/api/v1/corrections")
    assert response.status_code == 200
    assert isinstance(response.json(), dict)


def test_put_corrections_persists_for_next_get(client: TestClient) -> None:
    payload = {"kubectl": "kube control", "PostgreSQL": "post gres Q L"}
    with client:
        put = client.put("/api/v1/corrections", json=payload)
        assert put.status_code == 200
        assert put.json() == payload
        after = client.get("/api/v1/corrections").json()
    assert after == payload


def test_put_corrections_400_on_bad_entry(client: TestClient) -> None:
    with client:
        response = client.put(
            "/api/v1/corrections",
            json={"good": "ok", "": "empty key"},
        )
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "Corrections validation failed"
    assert any(f["key"] == "" for f in body["details"]["failures"])


def test_put_corrections_400_when_entry_count_exceeded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_CORRECTIONS_ENTRIES", "3")
    from app.config import get_settings

    get_settings.cache_clear()
    with client:
        response = client.put(
            "/api/v1/corrections",
            json={"a": "1", "b": "2", "c": "3", "d": "4"},
        )
    assert response.status_code == 400
    assert "too many entries" in str(response.json()["details"])


def test_put_prompt_rejects_blank_prompt(client: TestClient) -> None:
    """An empty or whitespace-only prompt slipping through would let the
    cleanup stage call the LLM with no system rules; the validator must
    reject it at the API layer."""

    with client:
        empty = client.put("/api/v1/prompt", json={"prompt": ""})
        whitespace = client.put("/api/v1/prompt", json={"prompt": "   \n\n  "})
    assert empty.status_code == 400
    assert whitespace.status_code == 400


def test_put_prompt_boundary_at_max_bytes_succeeds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly MAX_PROMPT_LENGTH_BYTES bytes must succeed; one over must 413.
    Guards against the `>` -> `>=` off-by-one in prompt_service.save."""

    monkeypatch.setenv("MAX_PROMPT_LENGTH_BYTES", "64")
    from app.config import get_settings

    get_settings.cache_clear()
    with client:
        exactly = client.put("/api/v1/prompt", json={"prompt": "x" * 64})
        over = client.put("/api/v1/prompt", json={"prompt": "x" * 65})
    assert exactly.status_code == 200, exactly.text
    assert over.status_code == 413


def test_put_corrections_typed_failure_envelope_on_non_string_value(
    client: TestClient,
) -> None:
    """Non-string values must surface as the per-key 'Corrections validation
    failed' envelope rather than the generic Pydantic 'Validation failed' --
    the typed validator owns value-shape checks now."""

    with client:
        response = client.put(
            "/api/v1/corrections",
            json={"kubectl": 123, "PostgreSQL": None},
        )
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "Corrections validation failed"
    failure_keys = {f["key"] for f in body["details"]["failures"]}
    assert {"kubectl", "PostgreSQL"}.issubset(failure_keys)
