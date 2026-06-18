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


def test_health_ready_render_component_present_and_not_gating(env: Path, monkeypatch) -> None:
    # Render is unconfigured by default: surfaced as a component (skipped =>
    # reachable) but never added to checks, so a down/absent sidecar cannot 503
    # readiness.
    _stub_probes(monkeypatch)
    with _client(env) as client:
        body = client.get("/health/ready").json()
    assert body["components"]["render"]["reachable"] is True
    assert body["components"]["render"]["url"] is None
    assert "render" not in body["checks"]


async def test_probe_render_returns_version(monkeypatch) -> None:
    import httpx
    from app.api import health as health_mod

    payload = {"ok": True, "version": "0.38.0"}
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json=payload))
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **kw: original(*a, **{**kw, "transport": transport})
    )
    status, detail = await health_mod._probe_render("http://render:8000", 2.0)
    assert status == "ok"
    assert detail["version"] == "0.38.0"


async def test_probe_render_skipped_when_unconfigured() -> None:
    from app.api import health as health_mod

    status, detail = await health_mod._probe_render("", 2.0)
    assert status == "skipped"
    assert detail == {}


def test_health_alias_returns_same_shape(env: Path, monkeypatch) -> None:
    _stub_probes(monkeypatch)
    with _client(env) as client:
        ready = client.get("/health/ready").json()
        alias = client.get("/health").json()
    assert ready["checks"] == alias["checks"]
    assert ready["components"] == alias["components"]


async def test_probe_tts_wrapper_surfaces_whisper_fields(monkeypatch) -> None:
    """The wrapper's whisper_* health fields are passed through to
    components.tts_wrapper so an operator can confirm ASR verification is loaded
    without reading the wrapper logs."""

    import httpx
    from app.api import health as health_mod

    payload = {
        "ok": True,
        "model_loaded": True,
        "reference_loaded": True,
        "version": "0.21.2",
        "engine": "chatterbox",
        "device": "cuda",
        "whisper_enabled": True,
        "whisper_model": "base",
        "whisper_loaded": True,
    }
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json=payload))
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **kw: original(*a, **{**kw, "transport": transport})
    )

    status, detail = await health_mod._probe_tts_wrapper("http://tts-wrapper:8000", 2.0)
    assert status == "ok"
    assert detail["whisper_enabled"] is True
    assert detail["whisper_model"] == "base"
    assert detail["whisper_loaded"] is True
    assert detail["engine"] == "chatterbox"  # existing fields still pass through


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
