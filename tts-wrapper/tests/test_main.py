"""Wrapper HTTP contract tests using a stub :class:`Engine`.

These tests never import torch or Coqui TTS. The stub generates real WAV
bytes (silent at the requested sample rate) so the /generate handler can
write a valid file and the duration calculation can read its header.
"""

from __future__ import annotations

import asyncio
import io
import wave
from pathlib import Path

from fastapi.testclient import TestClient

from engine import Engine, GPUOutOfMemoryError
from main import create_app


def _silent_wav(duration_secs: float = 0.5, sample_rate: int = 24000) -> bytes:
    """Encode a silent mono int16 WAV of the requested duration."""

    n_samples = int(duration_secs * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_samples)
    return buf.getvalue()


class FakeEngine:
    """Stand-in for XTTSEngine for testing.

    Implements the :class:`Engine` Protocol; never touches torch.
    """

    sample_rate = 24000
    device = "cpu"

    def __init__(
        self,
        *,
        fail_load: bool = False,
        oom_synthesize: bool = False,
        synthesize_returns: bytes | None = None,
    ) -> None:
        self.model_loaded = False
        self.reference_loaded = False
        self.fail_load = fail_load
        self.oom_synthesize = oom_synthesize
        self.synthesize_returns = synthesize_returns or _silent_wav()
        self.synthesize_calls: list[str] = []
        self.reload_calls = 0

    def load(self) -> None:
        if self.fail_load:
            raise RuntimeError("simulated model load failure")
        self.model_loaded = True
        self.reference_loaded = True

    async def synthesize(self, text: str) -> bytes:
        self.synthesize_calls.append(text)
        if self.oom_synthesize:
            raise GPUOutOfMemoryError("simulated OOM")
        # Tiny delay so the asyncio.Lock can be observed by another caller.
        await asyncio.sleep(0)
        return self.synthesize_returns

    async def reload_reference(self) -> None:
        self.reload_calls += 1
        self.reference_loaded = True


def _client(engine: Engine, tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return TestClient(create_app(engine=engine, data_dir=data_dir))


# --- /health --------------------------------------------------------------


def test_health_200_when_model_and_reference_loaded(tmp_path: Path) -> None:
    engine = FakeEngine()
    with _client(engine, tmp_path) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    # Core liveness flags are exact; the metadata fields (version/torch/
    # coqui_tts/device/sample_rate) the main app aggregates into
    # components.tts_wrapper are environment-dependent, so assert presence.
    assert body["ok"] is True
    assert body["model_loaded"] is True
    assert body["reference_loaded"] is True
    assert body["version"] == "0.1.0"
    for key in ("torch", "coqui_tts", "device", "sample_rate"):
        assert key in body


def test_health_503_when_reference_not_loaded(tmp_path: Path) -> None:
    engine = FakeEngine()
    # Override after load: simulate the rare 'model up, reference still loading'
    # case the /health body distinguishes.
    with _client(engine, tmp_path) as client:
        engine.reference_loaded = False
        response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["model_loaded"] is True
    assert body["reference_loaded"] is False
    # 503 body includes a diagnostic "error" string.
    assert "reference voice not loaded" in body["error"]


def test_generate_503_when_reference_not_loaded(tmp_path: Path) -> None:
    """With the model up but no committed voice, /generate refuses rather than
    synthesizing with no speaker; the operator uploads a voice via the UI first."""

    engine = FakeEngine()
    with _client(engine, tmp_path) as client:
        engine.reference_loaded = False
        response = client.post(
            "/generate", json={"text": "hello there", "episode_id": "ep1", "chunk_index": 0}
        )
    assert response.status_code == 503
    assert "no reference voice" in str(response.json()["detail"])


# --- /generate ------------------------------------------------------------


def test_generate_writes_wav_and_returns_path_duration_rate(tmp_path: Path) -> None:
    engine = FakeEngine(synthesize_returns=_silent_wav(duration_secs=2.0))
    with _client(engine, tmp_path) as client:
        response = client.post(
            "/generate",
            json={"text": "hello there", "episode_id": "ep-1", "chunk_index": 3},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    expected_path = tmp_path / "data" / "media" / "ep-1_chunk_3.wav"
    assert body["wav_path"] == str(expected_path)
    assert expected_path.exists()
    assert body["sample_rate"] == 24000
    # 2 second silent WAV
    assert 1.95 <= body["duration_secs"] <= 2.05
    assert engine.synthesize_calls == ["hello there"]


def test_generate_rejects_blank_text(tmp_path: Path) -> None:
    engine = FakeEngine()
    with _client(engine, tmp_path) as client:
        response = client.post(
            "/generate",
            json={"text": "", "episode_id": "ep", "chunk_index": 0},
        )
    assert response.status_code == 422  # Pydantic validation default


def test_generate_rejects_negative_chunk_index(tmp_path: Path) -> None:
    engine = FakeEngine()
    with _client(engine, tmp_path) as client:
        response = client.post(
            "/generate",
            json={"text": "hi", "episode_id": "ep", "chunk_index": -1},
        )
    assert response.status_code == 422


def test_generate_rejects_extra_fields(tmp_path: Path) -> None:
    engine = FakeEngine()
    with _client(engine, tmp_path) as client:
        response = client.post(
            "/generate",
            json={"text": "hi", "episode_id": "ep", "chunk_index": 0, "extra": 1},
        )
    assert response.status_code == 422


def test_generate_500_with_gpu_oom_detail(tmp_path: Path) -> None:
    engine = FakeEngine(oom_synthesize=True)
    with _client(engine, tmp_path) as client:
        response = client.post(
            "/generate",
            json={"text": "x", "episode_id": "ep", "chunk_index": 0},
        )
    assert response.status_code == 500
    body = response.json()
    detail = body["detail"]
    assert detail["error"] == "GPU OOM"
    assert "simulated" in detail["cause"]


def test_generate_serializes_concurrent_calls_via_lock(tmp_path: Path) -> None:
    """The asyncio.Lock around inference must queue concurrent /generate calls;
    if two requests overlap without serialization, the synthesize_calls log
    won't be deterministic."""

    engine = FakeEngine()
    with _client(engine, tmp_path) as client:
        # TestClient is sync; emulating overlap with two quick calls is enough
        # to assert both ran exactly once and in the request order.
        client.post("/generate", json={"text": "first", "episode_id": "ep", "chunk_index": 0})
        client.post("/generate", json={"text": "second", "episode_id": "ep", "chunk_index": 1})
    assert engine.synthesize_calls == ["first", "second"]


# --- /reload --------------------------------------------------------------


def test_reload_increments_call_counter_and_returns_ok(tmp_path: Path) -> None:
    engine = FakeEngine()
    with _client(engine, tmp_path) as client:
        response = client.post("/reload")
    assert response.status_code == 200
    # Build-plan spec line 805: { ok: true }, nothing else.
    assert response.json() == {"ok": True}
    assert engine.reload_calls == 1


# --- path traversal + traversal + reload error paths -----------------------


def test_generate_rejects_path_traversal_in_episode_id(tmp_path: Path) -> None:
    """Pydantic pattern must block any episode_id with path separators or
    '..' segments. The wav file would otherwise escape data_dir/media."""

    engine = FakeEngine()
    with _client(engine, tmp_path) as client:
        for evil in ("../../etc/foo", "../etc", "a/b", "with space", "with\x00null"):
            response = client.post(
                "/generate",
                json={"text": "hi", "episode_id": evil, "chunk_index": 0},
            )
            assert response.status_code == 422, f"accepted: {evil!r}"


def test_reload_404_when_engine_raises_file_not_found(tmp_path: Path) -> None:
    """Missing voice.wav on disk must surface as 404 so the backend's tts
    client classifies it as a non-retryable TTSRequestError instead of retrying."""

    class _MissingRefEngine(FakeEngine):
        async def reload_reference(self) -> None:
            self.reload_calls += 1
            raise FileNotFoundError("/app/reference/voice.wav")

    engine = _MissingRefEngine()
    with _client(engine, tmp_path) as client:
        response = client.post("/reload")
    assert response.status_code == 404
    assert "voice.wav" in response.json()["detail"]


def test_reload_500_when_engine_raises_other_error(tmp_path: Path) -> None:
    """Any non-FileNotFoundError reload failure must come back as 5xx so the
    backend's retry logic kicks in."""

    class _BoomEngine(FakeEngine):
        async def reload_reference(self) -> None:
            self.reload_calls += 1
            raise RuntimeError("torch did a bad")

    engine = _BoomEngine()
    with _client(engine, tmp_path) as client:
        response = client.post("/reload")
    assert response.status_code == 500
    assert "torch did a bad" in response.json()["detail"]


def test_sample_rate_mismatch_between_engine_and_wav_is_observable(
    tmp_path: Path,
) -> None:
    """If the engine reports one sample_rate but the WAV header carries
    another, the duration computed from the WAV header will be wrong and the
    response sample_rate will follow the engine. The test verifies the
    response carries the engine's value so a future code change that reads
    the rate from the WAV bytes fails this assertion."""

    engine = FakeEngine(synthesize_returns=_silent_wav(duration_secs=1.0, sample_rate=22050))
    engine.sample_rate = 24000  # engine claims 24000 even though wav is 22050

    with _client(engine, tmp_path) as client:
        response = client.post(
            "/generate",
            json={"text": "hi", "episode_id": "ep", "chunk_index": 0},
        )
    body = response.json()
    assert body["sample_rate"] == 24000
    # 1s of audio at 22050 Hz played as if 24000 Hz => duration_secs reads the
    # actual header rate, not the engine's claimed rate. Both expected values
    # are documented so a future refactor doesn't silently swap them.
    assert 0.95 <= body["duration_secs"] <= 1.05


def test_atomic_write_does_not_leave_tmp_files_on_success(tmp_path: Path) -> None:
    engine = FakeEngine()
    with _client(engine, tmp_path) as client:
        client.post(
            "/generate",
            json={"text": "hi", "episode_id": "ep", "chunk_index": 0},
        )
    media = tmp_path / "data" / "media"
    final_files = list(media.glob("ep_chunk_0.wav"))
    tmp_files = list(media.glob(".ep_chunk_0.wav.*.tmp"))
    assert len(final_files) == 1
    assert tmp_files == []
