"""Wrapper HTTP contract tests using a stub :class:`Engine`.

These tests never import torch or the TTS model library. The stub generates real
WAV bytes (silent at the requested sample rate) so the /generate handler can
write a valid file and the duration calculation can read its header.
"""

from __future__ import annotations

import asyncio
import io
import wave
from pathlib import Path

from fastapi.testclient import TestClient

from engine import Engine, GenerationParams, GPUOutOfMemoryError, InferenceBusyError
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
    """Stand-in for ChatterboxEngine for testing.

    Implements the :class:`Engine` Protocol; never touches torch.
    """

    sample_rate = 24000
    device = "cpu"

    def __init__(
        self,
        *,
        fail_load: bool = False,
        oom_synthesize: bool = False,
        busy_synthesize: bool = False,
        synthesize_returns: bytes | None = None,
    ) -> None:
        self.model_loaded = False
        self.reference_loaded = False
        self.fail_load = fail_load
        self.oom_synthesize = oom_synthesize
        self.busy_synthesize = busy_synthesize
        self.synthesize_returns = synthesize_returns or _silent_wav()
        self.synthesize_calls: list[str] = []
        self.reload_calls = 0

    def load(self) -> None:
        if self.fail_load:
            raise RuntimeError("simulated model load failure")
        self.model_loaded = True
        self.reference_loaded = True

    async def synthesize(self, text: str, params: GenerationParams) -> bytes:
        self.synthesize_calls.append(text)
        self.last_params = params
        if self.oom_synthesize:
            raise GPUOutOfMemoryError("simulated OOM")
        if self.busy_synthesize:
            raise InferenceBusyError("simulated busy")
        # Tiny delay so the asyncio.Lock can be observed by another caller.
        await asyncio.sleep(0)
        return self.synthesize_returns

    async def reload_reference(self) -> None:
        self.reload_calls += 1
        self.reference_loaded = True


class FakeVerifier:
    """Stand-in for WhisperVerifier; never imports faster-whisper."""

    def __init__(self, *, returns: str = "transcribed text", raises: bool = False) -> None:
        self.model_name = "fake"
        self.loaded = False
        self.load_calls = 0
        self.returns = returns
        self.raises = raises
        self.calls: list[bytes] = []

    def load(self) -> None:
        self.load_calls += 1
        self.loaded = True

    def transcribe(self, wav_bytes: bytes, language: str = "en") -> str:
        self.calls.append(wav_bytes)
        if self.raises:
            raise RuntimeError("simulated whisper failure")
        return self.returns


def _client(engine: Engine, tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return TestClient(create_app(engine=engine, data_dir=data_dir))


def _client_with_verifier(engine: Engine, verifier: FakeVerifier, tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return TestClient(create_app(engine=engine, data_dir=data_dir, verifier=verifier))


# --- /health --------------------------------------------------------------


def test_health_200_when_model_and_reference_loaded(tmp_path: Path) -> None:
    engine = FakeEngine()
    with _client(engine, tmp_path) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    # Core liveness flags are exact; the metadata fields (version/torch/device/
    # sample_rate) the main app aggregates into components.tts_wrapper are
    # environment-dependent, so assert presence.
    assert body["ok"] is True
    assert body["model_loaded"] is True
    assert body["reference_loaded"] is True
    # Version is read from the repo-root VERSION file (or AUDICLE_WRAPPER_VERSION
    # in the image); assert against that file so the test never hard-codes a
    # literal that drifts on every release.
    version_file = Path(__file__).resolve().parents[2] / "VERSION"
    assert body["version"] == version_file.read_text(encoding="utf-8").strip()
    for key in ("torch", "device", "sample_rate"):
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


def test_health_live_200_without_reference(tmp_path: Path) -> None:
    """Liveness is satisfied by the model alone. A voice-less wrapper returns
    503 on /health (readiness) but must be 200 on /health/live, or the app's
    depends_on(service_healthy) deadlocks waiting for a voice that can only be
    uploaded after the app starts."""

    engine = FakeEngine()
    with _client(engine, tmp_path) as client:
        engine.reference_loaded = False
        live = client.get("/health/live")
        ready = client.get("/health")
    assert live.status_code == 200
    assert live.json()["model_loaded"] is True
    assert ready.status_code == 503


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
    # With no verifier wired and verify not requested, transcript is null.
    assert body["transcript"] is None


def test_verifier_warmed_at_startup(tmp_path: Path) -> None:
    engine = FakeEngine()
    verifier = FakeVerifier()
    with _client_with_verifier(engine, verifier, tmp_path) as client:
        # Entering the TestClient context runs lifespan startup, which warms the
        # model so the first /generate doesn't pay the load cost.
        assert verifier.load_calls == 1
        assert verifier.loaded is True
        body = client.get("/health").json()
    assert body["whisper_loaded"] is True


def test_generate_verify_returns_transcript(tmp_path: Path) -> None:
    engine = FakeEngine()
    verifier = FakeVerifier(returns="hello there")
    with _client_with_verifier(engine, verifier, tmp_path) as client:
        response = client.post(
            "/generate",
            json={"text": "hello there", "episode_id": "ep-1", "chunk_index": 0, "verify": True},
        )
    assert response.status_code == 200, response.text
    assert response.json()["transcript"] == "hello there"
    assert len(verifier.calls) == 1  # transcribed the produced audio once


def test_generate_without_verify_skips_transcription(tmp_path: Path) -> None:
    engine = FakeEngine()
    verifier = FakeVerifier()
    with _client_with_verifier(engine, verifier, tmp_path) as client:
        response = client.post(
            "/generate",
            json={"text": "hello there", "episode_id": "ep-1", "chunk_index": 0},
        )
    assert response.status_code == 200
    assert response.json()["transcript"] is None
    assert verifier.calls == []  # verify not requested => verifier untouched


def test_generate_verify_failure_does_not_fail_chunk(tmp_path: Path) -> None:
    engine = FakeEngine()
    verifier = FakeVerifier(raises=True)
    with _client_with_verifier(engine, verifier, tmp_path) as client:
        response = client.post(
            "/generate",
            json={"text": "hello there", "episode_id": "ep-1", "chunk_index": 0, "verify": True},
        )
    # ASR failure degrades to no transcript; the chunk still succeeds.
    assert response.status_code == 200
    assert response.json()["transcript"] is None


def test_generate_forwards_request_knobs_to_engine(tmp_path: Path) -> None:
    # 0.44.0: the backend's runtime settings ride on every call; the wrapper
    # must hand them to the engine verbatim.
    engine = FakeEngine()
    with _client(engine, tmp_path) as client:
        response = client.post(
            "/generate",
            json={
                "text": "hi",
                "episode_id": "ep",
                "chunk_index": 0,
                "seed": 7,
                "temperature": 0.9,
                "repetition_penalty": 1.5,
                "top_p": 0.8,
                "top_k": 50,
                "max_chars": 500,
            },
        )
    assert response.status_code == 200
    assert engine.last_params == GenerationParams(
        seed=7, temperature=0.9, repetition_penalty=1.5, top_p=0.8, top_k=50, max_chars=500
    )


def test_generate_omitted_knobs_fall_back_to_defaults(tmp_path: Path) -> None:
    # A hand-curated request (or an older backend) that omits the knobs must
    # get the GenerationParams defaults, not None.
    engine = FakeEngine()
    with _client(engine, tmp_path) as client:
        response = client.post(
            "/generate",
            json={"text": "hi", "episode_id": "ep", "chunk_index": 0},
        )
    assert response.status_code == 200
    assert engine.last_params == GenerationParams()


def test_generate_rejects_out_of_range_knobs(tmp_path: Path) -> None:
    # Bounds fail loudly (422) instead of degrading audio silently.
    engine = FakeEngine()
    bad_payloads = [
        {"temperature": 0},
        {"temperature": 2.5},
        {"repetition_penalty": 0.5},
        {"top_p": 1.5},
        {"top_k": 0},
        {"max_chars": 50},
        {"max_chars": 5000},
        {"seed": -1},
    ]
    with _client(engine, tmp_path) as client:
        for bad in bad_payloads:
            response = client.post(
                "/generate",
                json={"text": "hi", "episode_id": "ep", "chunk_index": 0, **bad},
            )
            assert response.status_code == 422, f"accepted: {bad}"


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


def test_generate_503_when_inference_busy(tmp_path: Path) -> None:
    """An overlapping inference (e.g. a backend retry while an orphaned post-
    timeout thread is still on the GPU) must come back as 503, not start a
    second concurrent GPU inference. 503 is server-side retryable so the backend
    backs off instead of stacking work."""

    engine = FakeEngine(busy_synthesize=True)
    with _client(engine, tmp_path) as client:
        response = client.post(
            "/generate",
            json={"text": "hi", "episode_id": "ep", "chunk_index": 0},
        )
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "inference busy"


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


def test_reload_503_when_inference_busy(tmp_path: Path) -> None:
    """If an inference (e.g. an orphaned post-timeout thread) still holds the GPU
    when /reload tries to recompute embeddings, the wrapper returns 503 rather
    than running concurrent GPU work or a misleading 500."""

    class _BusyReloadEngine(FakeEngine):
        async def reload_reference(self) -> None:
            self.reload_calls += 1
            raise InferenceBusyError("simulated busy")

    engine = _BusyReloadEngine()
    with _client(engine, tmp_path) as client:
        response = client.post("/reload")
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "inference busy"


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
