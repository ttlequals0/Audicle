from __future__ import annotations

import io
import shutil
import subprocess
import wave
from pathlib import Path

import pytest
from app.core import database
from app.main import create_app
from app.services import voices
from fastapi.testclient import TestClient


def _wav(duration_secs: float = 10.0, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * duration_secs))
    return buf.getvalue()


def _mp3(duration_secs: float = 10.0) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "wav", "-i", "pipe:0",
         "-c:a", "libmp3lame", "-f", "mp3", "pipe:1"],
        input=_wav(duration_secs),
        capture_output=True,
        check=True,
    ).stdout


@pytest.fixture
def client(env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Slot files write to a temp dir, never the repo.
    d = tmp_path / "voices"
    d.mkdir()
    monkeypatch.setattr(voices, "voices_dir", lambda: d)
    database.run_migrations(env)
    return TestClient(create_app())


def test_slots_start_empty(client: TestClient) -> None:
    slots = client.get("/api/v1/reference/slots").json()
    assert len(slots) == 5
    assert all(not s["filled"] for s in slots)


def test_upload_label_list_clear(client: TestClient) -> None:
    r = client.post(
        "/api/v1/reference/slots/2",
        files={"voice": ("v.wav", _wav(), "audio/wav")},
        data={"label": "Morgan"},
    )
    assert r.status_code == 200

    slots = {s["slot"]: s for s in client.get("/api/v1/reference/slots").json()}
    assert slots[2]["filled"] is True
    assert slots[2]["label"] == "Morgan"
    assert slots[2]["duration_secs"] == 10

    # rename
    client.put("/api/v1/reference/slots/2/label", data={"label": "Alex"})
    assert {s["slot"]: s for s in client.get("/api/v1/reference/slots").json()}[2]["label"] == "Alex"

    # preview serves the clip
    assert client.get("/api/v1/reference/slots/2/preview").status_code == 200

    # clear empties it and drops the label
    assert client.delete("/api/v1/reference/slots/2").status_code == 200
    after = {s["slot"]: s for s in client.get("/api/v1/reference/slots").json()}[2]
    assert after["filled"] is False
    assert after["label"] is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required for mp3 transcode")
def test_upload_mp3_slot_converted(client: TestClient) -> None:
    r = client.post(
        "/api/v1/reference/slots/1",
        files={"voice": ("v.mp3", _mp3(), "audio/mpeg")},
    )
    assert r.status_code == 200
    assert r.json()["filled"] is True
    # The slot is stored as a converted WAV, previewable as audio/wav.
    prev = client.get("/api/v1/reference/slots/1/preview")
    assert prev.status_code == 200
    assert prev.content.startswith(b"RIFF")


def test_upload_rejects_bad_wav(client: TestClient) -> None:
    r = client.post(
        "/api/v1/reference/slots/1",
        files={"voice": ("x.wav", b"not a wav", "audio/wav")},
    )
    assert r.status_code == 400


def test_slot_out_of_range_rejected(client: TestClient) -> None:
    # The app maps request-validation errors to 400 (its convention).
    assert client.get("/api/v1/reference/slots/9/preview").status_code == 400


def test_preview_404_when_empty(client: TestClient) -> None:
    assert client.get("/api/v1/reference/slots/3/preview").status_code == 404


def test_safe_slot_path_stays_under_voices_dir(client: TestClient) -> None:
    # The CodeQL-recognized containment barrier: a slot path always resolves to
    # slotN.wav under the voices dir, never escapes it.
    from app.api.v1 import reference

    p = reference._safe_slot_path(2)
    assert p.is_relative_to(voices.voices_dir().resolve())
    assert p.name == "slot2.wav"
