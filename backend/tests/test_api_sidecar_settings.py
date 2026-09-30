from __future__ import annotations

from pathlib import Path

from app.core import database
from app.main import create_app
from app.services import sidecar_settings
from fastapi.testclient import TestClient


def test_sidecar_settings_persist_desired_and_apply_tts_config(env: Path, monkeypatch) -> None:
    database.run_migrations(env)
    wrapper_defaults = {
        "TTS_MEMORY_SOFT_LIMIT_MB": 8000,
        "TTS_MEMORY_HARD_LIMIT_MB": 14000,
        "TTS_IDLE_UNLOAD_SECONDS": 300,
        "WHISPER_ENABLED": False,
        "WHISPER_MODEL": "base",
        "WHISPER_DEVICE": "cuda",
        "WHISPER_COMPUTE_TYPE": "float16",
        "TTS_REQUEST_TIMEOUT_SECONDS": 120,
    }
    wrapper_effective = dict(wrapper_defaults)
    applied: list[dict] = []

    async def fetch_config(_base_url: str, sidecar: str):
        if sidecar == "render":
            return {"defaults": {"RENDER_NAV_TIMEOUT_MS": 45000}}
        return {"defaults": wrapper_defaults, "effective": wrapper_effective}

    async def apply_config(_base_url: str, _sidecar: str, values: dict):
        applied.append(values)
        wrapper_effective.update(values)
        return {"defaults": wrapper_defaults, "effective": wrapper_effective, "values": values}

    monkeypatch.setattr(sidecar_settings, "fetch_config", fetch_config)
    monkeypatch.setattr(sidecar_settings, "apply_config", apply_config)
    with TestClient(create_app()) as client:
        response = client.put(
            "/api/v1/settings/sidecars",
            json={
                "render": {"RENDER_NAV_TIMEOUT_MS": 60000},
                "tts_wrapper": {
                    "TTS_MEMORY_SOFT_LIMIT_MB": 9000,
                    "TTS_MEMORY_HARD_LIMIT_MB": 15000,
                    "WHISPER_ENABLED": True,
                },
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["render"]["values"]["RENDER_NAV_TIMEOUT_MS"] == 60000
    assert body["render"]["pending"] is False
    assert body["tts_wrapper"]["pending"] is False
    assert applied == [
        {
            "TTS_MEMORY_SOFT_LIMIT_MB": 9000,
            "TTS_MEMORY_HARD_LIMIT_MB": 15000,
            "WHISPER_ENABLED": True,
        }
    ]


def test_sidecar_settings_report_pending_when_wrapper_unavailable(env: Path, monkeypatch) -> None:
    database.run_migrations(env)

    async def unavailable(*_):
        return None

    monkeypatch.setattr(sidecar_settings, "fetch_config", unavailable)
    with TestClient(create_app()) as client:
        response = client.put(
            "/api/v1/settings/sidecars",
            json={"tts_wrapper": {"TTS_MEMORY_SOFT_LIMIT_MB": 9000}},
        )
    assert response.status_code == 200
    wrapper = response.json()["tts_wrapper"]
    assert wrapper["available"] is False
    assert wrapper["pending"] is True
    assert wrapper["values"] == {"TTS_MEMORY_SOFT_LIMIT_MB": 9000}


def test_sidecar_reset_applies_empty_authoritative_override_map(env: Path, monkeypatch) -> None:
    database.run_migrations(env)
    defaults = {"TTS_MEMORY_SOFT_LIMIT_MB": 8000, "TTS_MEMORY_HARD_LIMIT_MB": 14000}
    effective = dict(defaults)
    applied: list[dict] = []

    async def fetch_config(_base_url: str, _sidecar: str):
        return {"defaults": defaults, "effective": effective}

    async def apply_config(_base_url: str, _sidecar: str, values: dict):
        applied.append(values)
        effective.clear()
        effective.update(defaults)
        effective.update(values)
        return {"defaults": defaults, "effective": effective, "values": values}

    monkeypatch.setattr(sidecar_settings, "fetch_config", fetch_config)
    monkeypatch.setattr(sidecar_settings, "apply_config", apply_config)
    with TestClient(create_app()) as client:
        set_value = client.put(
            "/api/v1/settings/sidecars",
            json={"tts_wrapper": {"TTS_MEMORY_SOFT_LIMIT_MB": 7000}},
        )
        reset = client.put(
            "/api/v1/settings/sidecars",
            json={"tts_wrapper": {"TTS_MEMORY_SOFT_LIMIT_MB": None}},
        )
    assert set_value.status_code == 200
    assert reset.status_code == 200
    assert reset.json()["tts_wrapper"]["values"] == {}
    assert reset.json()["tts_wrapper"]["pending"] is False
    assert applied == [{"TTS_MEMORY_SOFT_LIMIT_MB": 7000}, {}]


def test_sidecar_settings_reject_unknown_and_invalid_values(env: Path) -> None:
    database.run_migrations(env)
    with TestClient(create_app()) as client:
        unknown = client.put(
            "/api/v1/settings/sidecars", json={"tts_wrapper": {"DATA_DIR": "/tmp"}}
        )
        invalid = client.put(
            "/api/v1/settings/sidecars", json={"render": {"RENDER_ATTEMPTS": 0}}
        )
        fractional = client.put(
            "/api/v1/settings/sidecars", json={"render": {"RENDER_ATTEMPTS": 1.5}}
        )
        malformed = client.put(
            "/api/v1/settings/sidecars", json={"tts_wrapper": {"WHISPER_DEVICE": []}}
        )
    assert unknown.status_code == 400
    assert invalid.status_code == 400
    assert fractional.status_code == 400
    assert malformed.status_code == 400
