from __future__ import annotations

from pathlib import Path

from app.core import database
from app.main import create_app
from fastapi.testclient import TestClient


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def test_get_settings_returns_empty_values_initially(env: Path) -> None:
    with _client(env) as client:
        response = client.get("/api/v1/settings")
    assert response.status_code == 200
    body = response.json()
    assert "RETENTION_DAYS" in body["allowlist"]
    assert body["values"] == {}


def test_get_returns_effective_defaults(env: Path) -> None:
    """The GET response carries a `defaults` map so the UI shows editable
    effective defaults; secret keys are masked, not leaked."""

    with _client(env) as client:
        body = client.get("/api/v1/settings").json()
    defaults = body["defaults"]
    assert defaults["LLM_PROVIDER"] == "openai-compatible"
    assert defaults["LLM_TEMPERATURE"] == 0.7
    assert defaults["RETENTION_DAYS"] == 90
    assert defaults["OLLAMA_BASE_URL"].startswith("http")
    # Secret keys are masked even in defaults (env sets OPENAI_API_KEY).
    assert defaults["OPENAI_API_KEY"] in ("", "********")
    assert defaults["OPENROUTER_API_KEY"] in ("", "********")


def test_connection_urls_are_allowlisted_and_round_trip(env: Path) -> None:
    """FIRECRAWL_URL + TTS_URL are operator-tunable so an external Firecrawl/TTS
    can be configured without an env edit + restart."""

    with _client(env) as client:
        assert "FIRECRAWL_URL" in client.get("/api/v1/settings").json()["allowlist"]
        assert "TTS_URL" in client.get("/api/v1/settings").json()["allowlist"]
        response = client.put(
            "/api/v1/settings",
            json={
                "FIRECRAWL_URL": "http://firecrawl.other:3002",
                "TTS_URL": "http://tts.other:8000",
            },
        )
    assert response.status_code == 200
    values = response.json()["values"]
    assert values["FIRECRAWL_URL"] == "http://firecrawl.other:3002"
    assert values["TTS_URL"] == "http://tts.other:8000"


def test_put_settings_persists_and_coerces_types(env: Path) -> None:
    with _client(env) as client:
        response = client.put(
            "/api/v1/settings",
            json={
                "RETENTION_DAYS": 30,
                "FEED_TITLE": "My Custom Feed",
                "FEED_EXPLICIT": True,
            },
        )
    assert response.status_code == 200
    values = response.json()["values"]
    assert values["RETENTION_DAYS"] == 30
    assert values["FEED_TITLE"] == "My Custom Feed"
    assert values["FEED_EXPLICIT"] is True


def test_put_settings_rejects_unknown_keys(env: Path) -> None:
    with _client(env) as client:
        response = client.put(
            "/api/v1/settings",
            json={"DATA_DIR": "/tmp/evil"},
        )
    assert response.status_code == 400
    assert "DATA_DIR" in response.json()["error"]


def test_put_then_get_round_trips(env: Path) -> None:
    with _client(env) as client:
        client.put("/api/v1/settings", json={"FEED_AUTHOR": "New Owner"})
        response = client.get("/api/v1/settings")
    assert response.json()["values"]["FEED_AUTHOR"] == "New Owner"


def test_llm_provider_group_is_editable(env: Path) -> None:
    with _client(env) as client:
        response = client.put(
            "/api/v1/settings",
            json={"LLM_PROVIDER": "anthropic", "LLM_MODEL": "claude-x", "LLM_TEMPERATURE": 0.5},
        )
    assert response.status_code == 200
    values = response.json()["values"]
    assert values["LLM_PROVIDER"] == "anthropic"
    assert values["LLM_MODEL"] == "claude-x"
    assert values["LLM_TEMPERATURE"] == 0.5


def test_api_key_is_masked_on_get_and_survives_resave(env: Path) -> None:
    """A stored secret never echoes back; re-saving the form (which sends the
    mask sentinel) must not clobber the real value."""

    from app.services import runtime_settings

    with _client(env) as client:
        client.put("/api/v1/settings", json={"OPENAI_API_KEY": "sk-secret-123"})
        masked = client.get("/api/v1/settings").json()["values"]["OPENAI_API_KEY"]
        assert masked == runtime_settings.MASK_SENTINEL
        # Re-save with the sentinel (as the UI would) -> stored value unchanged.
        client.put("/api/v1/settings", json={"OPENAI_API_KEY": runtime_settings.MASK_SENTINEL})

    # The real value is still in the DB (overlay would resolve it), not the mask.
    conn = database.connect(database.db_path(env))
    try:
        stored = runtime_settings.get_all(conn)
    finally:
        conn.close()
    assert stored["OPENAI_API_KEY"] == "sk-secret-123"


def test_firecrawl_api_key_is_allowlisted_and_masked(env: Path) -> None:
    """The Firecrawl key is operator-settable and treated as a secret: allowlisted
    for PUT, never echoed back, and survives a sentinel re-save like the LLM keys."""

    from app.services import runtime_settings

    with _client(env) as client:
        assert "FIRECRAWL_API_KEY" in client.get("/api/v1/settings").json()["allowlist"]
        client.put("/api/v1/settings", json={"FIRECRAWL_API_KEY": "fc-secret-123"})
        masked = client.get("/api/v1/settings").json()["values"]["FIRECRAWL_API_KEY"]
        assert masked == runtime_settings.MASK_SENTINEL
        client.put(
            "/api/v1/settings", json={"FIRECRAWL_API_KEY": runtime_settings.MASK_SENTINEL}
        )

    conn = database.connect(database.db_path(env))
    try:
        stored = runtime_settings.get_all(conn)
    finally:
        conn.close()
    assert stored["FIRECRAWL_API_KEY"] == "fc-secret-123"


def test_api_key_cleared_by_empty_value(env: Path) -> None:
    """Sending an empty string for a masked key removes the override."""

    from app.services import runtime_settings

    with _client(env) as client:
        client.put("/api/v1/settings", json={"OPENAI_API_KEY": "sk-to-clear"})
        client.put("/api/v1/settings", json={"OPENAI_API_KEY": ""})

    conn = database.connect(database.db_path(env))
    try:
        stored = runtime_settings.get_all(conn)
    finally:
        conn.close()
    assert "OPENAI_API_KEY" not in stored


def test_api_key_overlay_reaches_settings(env: Path) -> None:
    """An LLM override stored via the API is applied by overlay() -- the same
    resolution the worker now runs per job."""

    from app.config import get_settings
    from app.services import runtime_settings

    with _client(env) as client:
        client.put("/api/v1/settings", json={"OPENAI_API_KEY": "sk-overlaid", "LLM_MODEL": "m2"})

    overlaid = runtime_settings.overlay(get_settings())
    assert overlaid.OPENAI_API_KEY == "sk-overlaid"
    assert overlaid.LLM_MODEL == "m2"
