from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import soundfile as sf
from app.core import database
from app.main import create_app
from fastapi.testclient import TestClient


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def _wav_bytes(seconds: float = 1.0, rate: int = 24000) -> bytes:
    tone = np.zeros(int(seconds * rate), dtype="float32")
    buf = io.BytesIO()
    sf.write(buf, tone, rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def test_chime_absent_by_default(env: Path) -> None:
    with _client(env) as client:
        r = client.get("/api/v1/chime")
    assert r.status_code == 200
    assert r.json() == {"present": False, "duration_secs": None}


def test_chime_upload_preview_delete_roundtrip(env: Path) -> None:
    with _client(env) as client:
        up = client.post(
            "/api/v1/chime", files={"file": ("chime.wav", _wav_bytes(2.0), "audio/wav")}
        )
        assert up.status_code == 201
        body = up.json()
        assert body["present"] is True
        assert body["duration_secs"] == 2

        got = client.get("/api/v1/chime").json()
        assert got["present"] is True

        preview = client.get("/api/v1/chime/preview")
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "audio/wav"

        assert client.delete("/api/v1/chime").status_code == 204
        assert client.get("/api/v1/chime").json()["present"] is False


def test_chime_rejects_empty_upload(env: Path) -> None:
    with _client(env) as client:
        r = client.post("/api/v1/chime", files={"file": ("empty.wav", b"", "audio/wav")})
    assert r.status_code == 400
