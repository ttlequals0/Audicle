from __future__ import annotations

from pathlib import Path

from app.api.body_limit import UploadBodyLimitMiddleware
from app.config import get_settings
from app.main import create_app
from fastapi.testclient import TestClient


def test_advertised_oversize_is_rejected_before_multipart(env: Path) -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/chime/",
            content=b"x",
            headers={"Content-Length": str(12 * 1024 * 1024)},
        )
    assert response.status_code == 413
    assert response.json() == {"error": "request body too large", "status": 413}


def test_streamed_total_limit_covers_multiple_parts(env: Path, monkeypatch) -> None:
    monkeypatch.setattr(UploadBodyLimitMiddleware, "_limit", lambda _self, _path: 128)
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/chime",
            files=[("file", ("one.wav", b"a" * 80)), ("file", ("two.wav", b"b" * 80))],
        )
    assert response.status_code == 413
    assert response.json() == {"error": "request body too large", "status": 413}


def test_negative_content_length_is_rejected(env: Path) -> None:
    settings = get_settings()
    app = UploadBodyLimitMiddleware(create_app(), settings)
    client = TestClient(app)
    response = client.post("/api/v1/upload", content=b"x", headers={"Content-Length": "-1"})
    assert response.status_code == 413
