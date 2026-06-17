from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from app.config import get_settings
from app.core import database
from app.main import create_app
from fastapi.testclient import TestClient


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def test_test_endpoint_409_without_url(env: Path) -> None:
    with _client(env) as client:
        r = client.post("/api/v1/webhooks/test")
    assert r.status_code == 409


def test_test_endpoint_delivers(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_URL", "https://hook.test/x")
    get_settings.cache_clear()
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: original(*a, **{**k, "transport": transport})
    )
    with _client(env) as client:
        r = client.post("/api/v1/webhooks/test")
    assert r.status_code == 200
    body = r.json()
    assert body == {"delivered": True, "status_code": 200, "error": None}
