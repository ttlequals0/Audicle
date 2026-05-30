from __future__ import annotations

from pathlib import Path

from app.core import database
from app.main import create_app
from app.services import runtime_settings
from fastapi.testclient import TestClient


def _client(data_dir: Path) -> TestClient:
    database.run_migrations(data_dir)
    return TestClient(create_app())


def test_health_live(env: Path) -> None:
    with _client(env) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["version"]
    # uptime + base_url power the Settings system-info / feed URL display.
    assert isinstance(body["uptime_seconds"], int)
    assert body["uptime_seconds"] >= 0
    assert body["base_url"] == "https://audifeed.example.test"


def _stub_probes(monkeypatch) -> None:
    """Health aggregation hits real network for TTS/Firecrawl/LLM; in tests
    the configured URLs are sinkholes. Stub the probe + ffmpeg banner so the
    happy-path assertion isolates the DB check."""

    from app.api import health as health_mod

    async def _ok(*_a, **_kw):
        return "ok"

    async def _ok_tts(*_a, **_kw):
        return "ok", {"version": "0.1.0", "device": "cpu", "model_loaded": True}

    monkeypatch.setattr(health_mod, "_probe_http", _ok)
    monkeypatch.setattr(health_mod, "_probe_tts_wrapper", _ok_tts)
    monkeypatch.setattr(health_mod, "_ffmpeg_version", lambda: "stub")


def test_health_ready_ok(env: Path, monkeypatch) -> None:
    _stub_probes(monkeypatch)
    with _client(env) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["checks"]["db"] == "ok"
    assert body["components"]["app"]
    assert body["components"]["python"]


def test_health_ready_reflects_runtime_settings_overlay(env: Path, monkeypatch) -> None:
    # Regression: /health/ready must report the operator's UI-set LLM model
    # (stored in runtime_settings), not the empty env default. The bug read base
    # settings without applying the runtime overlay.
    _stub_probes(monkeypatch)
    database.run_migrations(env)
    with database.connection(env) as conn:
        runtime_settings.set_value(conn, "LLM_MODEL", "operator-chosen-model")
    with _client(env) as client:
        body = client.get("/health/ready").json()
    assert body["components"]["llm"]["model"] == "operator-chosen-model"


def test_health_alias_returns_same_shape(env: Path, monkeypatch) -> None:
    _stub_probes(monkeypatch)
    with _client(env) as client:
        ready = client.get("/health/ready").json()
        alias = client.get("/health").json()
    assert ready["checks"] == alias["checks"]
    assert ready["components"] == alias["components"]


def test_health_ready_503_when_db_unreachable(env: Path, monkeypatch) -> None:
    # Let the lifespan migration succeed against the real DB, then break only
    # the health endpoint's connect.
    database.run_migrations(env)

    def _broken_connect(*_args, **_kwargs):
        raise RuntimeError("simulated db failure")

    with TestClient(create_app()) as client:
        monkeypatch.setattr(database, "connect", _broken_connect)
        response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert "error" in body["checks"]["db"]
