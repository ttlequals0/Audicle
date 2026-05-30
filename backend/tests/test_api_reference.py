from __future__ import annotations

import io
import wave
from pathlib import Path

import httpx
import pytest
from app.core import database
from app.main import create_app
from fastapi.testclient import TestClient


def _wav_bytes(*, duration_secs: float = 10.0, sample_rate: int = 24000) -> bytes:
    """Synthesize a silent mono PCM-16 WAV of the given duration."""

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * int(sample_rate * duration_secs))
    return buf.getvalue()


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def test_preview_returns_404_when_no_reference_installed(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.v1 import reference

    monkeypatch.setattr(
        reference,
        "_reference_path",
        lambda: env / "voice_does_not_exist.wav",
    )
    with _client(env) as client:
        response = client.get("/api/v1/reference/preview")
    assert response.status_code == 404


def test_preview_serves_installed_clip(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.v1 import reference

    path = env / "voice.wav"
    path.write_bytes(_wav_bytes())
    monkeypatch.setattr(reference, "_reference_path", lambda: path)
    with _client(env) as client:
        response = client.get("/api/v1/reference/preview")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content.startswith(b"RIFF")


def test_commit_atomically_swaps_voice_and_calls_reload(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint validates the candidate WAV, replaces voice.wav, and
    POSTs /reload to the TTS wrapper."""

    from app.api.v1 import reference

    path = env / "voice.wav"
    path.write_bytes(b"OLDFAKE")
    monkeypatch.setattr(reference, "_reference_path", lambda: path)

    reload_calls: list[str] = []

    def _handler(request):
        reload_calls.append(str(request.url))
        return httpx.Response(200, json={"reloaded": True})

    transport = httpx.MockTransport(_handler)
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    new_clip = _wav_bytes(duration_secs=8.0)
    with _client(env) as client:
        response = client.post(
            "/api/v1/reference/commit",
            files={"voice": ("new.wav", new_clip, "audio/wav")},
        )

    assert response.status_code == 200
    body = response.json()
    # JSON booleans round-trip as Python bools but the route returns the
    # raw int from a literal; tolerate truthy.
    assert body["committed"]
    assert body["sample_rate"] == 24000
    assert body["duration_secs"] == 8
    assert path.read_bytes() == new_clip
    assert any("/reload" in url for url in reload_calls)


def test_audition_503_when_no_voice_committed(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.v1 import reference

    monkeypatch.setattr(reference, "_reference_path", lambda: env / "no_voice.wav")
    with _client(env) as client:
        response = client.post(
            "/api/v1/reference/audition", data={"sample_text": "hello there friend"}
        )
    assert response.status_code == 503


def test_audition_synthesizes_with_committed_voice(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.v1 import reference

    voice = env / "voice.wav"
    voice.write_bytes(_wav_bytes())
    monkeypatch.setattr(reference, "_reference_path", lambda: voice)

    # The wrapper writes the synthesized wav under DATA_DIR/media; point the
    # mocked /generate at a real file there so the containment check passes.
    media = env / "media"
    media.mkdir(parents=True, exist_ok=True)
    gen_wav = media / "audition_chunk_0.wav"
    gen_wav.write_bytes(_wav_bytes(duration_secs=3.0))

    def _handler(request):
        assert request.url.path.endswith("/generate")
        return httpx.Response(200, json={"wav_path": str(gen_wav)})

    transport = httpx.MockTransport(_handler)
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    with _client(env) as client:
        response = client.post(
            "/api/v1/reference/audition", data={"sample_text": "hello there friend"}
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content.startswith(b"RIFF")


def test_commit_rejects_too_short_clip(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.v1 import reference

    path = env / "voice.wav"
    monkeypatch.setattr(reference, "_reference_path", lambda: path)

    too_short = _wav_bytes(duration_secs=1.0)
    with _client(env) as client:
        response = client.post(
            "/api/v1/reference/commit",
            files={"voice": ("short.wav", too_short, "audio/wav")},
        )
    assert response.status_code == 400


def test_commit_rejects_oversized_clip(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.v1 import reference

    path = env / "voice.wav"
    monkeypatch.setattr(reference, "_reference_path", lambda: path)

    huge = b"\x00" * (6 * 1024 * 1024)
    with _client(env) as client:
        response = client.post(
            "/api/v1/reference/commit",
            files={"voice": ("huge.wav", huge, "audio/wav")},
        )
    assert response.status_code == 400


def test_commit_rejects_non_wav_payload(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.v1 import reference

    path = env / "voice.wav"
    monkeypatch.setattr(reference, "_reference_path", lambda: path)

    with _client(env) as client:
        response = client.post(
            "/api/v1/reference/commit",
            files={"voice": ("nope.txt", b"not a wav file at all", "audio/wav")},
        )
    assert response.status_code == 400


def test_default_sample_text_is_cicero_passage():
    from app.api.v1.reference import DEFAULT_SAMPLE_TEXT

    assert DEFAULT_SAMPLE_TEXT == (
        "But I must explain to you how all this mistaken idea of denouncing "
        "of a pleasure and praising pain was born and I will give you a "
        "complete account of the system, and expound the actual teachings of "
        "the great explorer of the truth, the master-builder of human happiness."
    )
    assert 4 <= len(DEFAULT_SAMPLE_TEXT) <= 400
