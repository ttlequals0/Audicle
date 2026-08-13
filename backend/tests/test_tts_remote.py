"""Remote OpenAI-compatible TTS and ASR backends (issue #117).

The structural point these cover: the bundled wrapper shares the /data volume
and returns a path, while a remote host returns bytes. The remote path has to
land at the same file the pipeline expects and produce the same result shape, or
every downstream stage breaks.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

import httpx
import pytest
from app.config import Settings, get_settings
from app.services import tts, tts_remote


def _wav_bytes(duration_secs: float = 1.0, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * duration_secs))
    return buf.getvalue()


def _patch_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    # synthesize/transcribe pull the shared client (module-level singleton in
    # tts.py), so patching httpx.AsyncClient itself no longer reaches them
    # once it's constructed. Hand back a dedicated client wired to this
    # test's transport instead.
    client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr(tts, "shared_client", lambda: client)


def _remote_env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> Settings:
    monkeypatch.setenv("TTS_BACKEND", "openai-api")
    monkeypatch.setenv("TTS_API_BASE_URL", "https://tts.example.test/v1")
    monkeypatch.setenv("WHISPER_BACKEND", "off")
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


# --- configuration guards --------------------------------------------------


def test_remote_tts_requires_a_base_url(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTS_BACKEND", "openai-api")
    monkeypatch.setenv("TTS_API_BASE_URL", "")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="TTS_API_BASE_URL"):
        get_settings()


def test_remote_tts_with_wrapper_whisper_is_rejected(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The combination that silently produces no transcripts: the local wrapper
    never receives audio synthesized elsewhere, so it cannot verify it."""

    monkeypatch.setenv("TTS_BACKEND", "openai-api")
    monkeypatch.setenv("TTS_API_BASE_URL", "https://tts.example.test/v1")
    monkeypatch.setenv("WHISPER_BACKEND", "wrapper")
    monkeypatch.setenv("WHISPER_VERIFY_ENABLED", "true")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="never receives it"):
        get_settings()


def test_remote_whisper_requires_a_base_url(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHISPER_BACKEND", "openai-api")
    monkeypatch.setenv("WHISPER_API_BASE_URL", "")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="WHISPER_API_BASE_URL"):
        get_settings()


def test_whisper_backend_off_disables_verification(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHISPER_VERIFY_ENABLED", "true")
    monkeypatch.setenv("WHISPER_BACKEND", "off")
    get_settings.cache_clear()
    assert get_settings().verification_enabled is False


def test_wrapper_defaults_are_unchanged(env: Path) -> None:
    """The bundled path stays the default so an existing deployment is
    untouched by this feature."""

    get_settings.cache_clear()
    settings = get_settings()
    assert settings.TTS_BACKEND == "wrapper"
    assert settings.WHISPER_BACKEND == "wrapper"


# --- synthesis -------------------------------------------------------------


async def test_remote_synthesis_writes_the_path_the_pipeline_expects(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote host shares no volume, so the bytes it returns must be written
    to exactly the filename the wrapper would have produced."""

    settings = _remote_env(monkeypatch)
    audio = _wav_bytes(duration_secs=2.0)

    def handler(request):
        assert request.url.path.endswith("/audio/speech")
        return httpx.Response(200, content=audio)

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    result = await tts.generate_chunk("hello", "ep1", 5, settings)

    assert result.wav_path.endswith("ep1_chunk_5.wav")
    assert Path(result.wav_path).read_bytes() == audio
    assert 1.95 <= result.duration_secs <= 2.05
    assert result.sample_rate == 24000
    # No transcript: a speech endpoint returns audio only.
    assert result.transcript is None


async def test_remote_synthesis_sends_model_and_voice(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _remote_env(
        monkeypatch, TTS_API_MODEL="chatterbox", TTS_API_VOICE="narrator", TTS_API_KEY="secret"
    )
    seen: dict = {}

    def handler(request):
        import json as _json

        seen.update(_json.loads(request.content))
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, content=_wav_bytes())

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    await tts.generate_chunk("hello", "ep1", 0, settings)

    assert seen["model"] == "chatterbox"
    assert seen["voice"] == "narrator"
    assert seen["input"] == "hello"
    assert seen["response_format"] == "wav"
    assert seen["auth"] == "Bearer secret"


async def test_remote_synthesis_maps_5xx_to_a_retryable_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _remote_env(monkeypatch)
    _patch_client(monkeypatch, httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(tts.TTSProviderError):
        await tts.generate_chunk("hello", "ep1", 0, settings)


async def test_remote_synthesis_maps_4xx_to_a_permanent_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected request must not burn the retry budget."""

    settings = _remote_env(monkeypatch)
    _patch_client(monkeypatch, httpx.MockTransport(lambda r: httpx.Response(400, text="nope")))
    with pytest.raises(tts.TTSRequestError):
        await tts.generate_chunk("hello", "ep1", 0, settings)


async def test_remote_synthesis_rejects_non_wav_audio(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline concatenates PCM WAV; an MP3 body would fail much later and
    much less legibly."""

    settings = _remote_env(monkeypatch)
    _patch_client(monkeypatch, httpx.MockTransport(lambda r: httpx.Response(200, content=b"ID3junk")))
    with pytest.raises(tts.TTSRequestError, match="not WAV"):
        await tts.generate_chunk("hello", "ep1", 0, settings)


async def test_remote_synthesis_rejects_an_empty_body(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _remote_env(monkeypatch)
    _patch_client(monkeypatch, httpx.MockTransport(lambda r: httpx.Response(200, content=b"")))
    with pytest.raises(tts.TTSProviderError):
        await tts.generate_chunk("hello", "ep1", 0, settings)


# --- remote ASR ------------------------------------------------------------


async def test_remote_transcription_returns_the_text(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _remote_env(
        monkeypatch,
        WHISPER_BACKEND="openai-api",
        WHISPER_API_BASE_URL="https://asr.example.test/v1",
        WHISPER_VERIFY_ENABLED="true",
    )
    audio = _wav_bytes()

    def handler(request):
        if request.url.path.endswith("/audio/speech"):
            return httpx.Response(200, content=audio)
        assert request.url.path.endswith("/audio/transcriptions")
        return httpx.Response(200, json={"text": "hello there"})

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    result = await tts.generate_chunk("hello there", "ep1", 0, settings, verify=True)
    assert result.transcript == "hello there"


async def test_remote_transcription_failure_does_not_fail_the_chunk(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verification is a quality gate, not a correctness requirement: an ASR
    outage degrades to no transcript, exactly as the wrapper path does."""

    settings = _remote_env(
        monkeypatch,
        WHISPER_BACKEND="openai-api",
        WHISPER_API_BASE_URL="https://asr.example.test/v1",
        WHISPER_VERIFY_ENABLED="true",
    )

    def handler(request):
        if request.url.path.endswith("/audio/speech"):
            return httpx.Response(200, content=_wav_bytes())
        return httpx.Response(500, text="asr down")

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    result = await tts.generate_chunk("hello", "ep1", 0, settings, verify=True)
    assert result.transcript is None
    assert result.wav_path.endswith("ep1_chunk_0.wav")


async def test_remote_transcription_tolerates_an_unexpected_shape(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _remote_env(
        monkeypatch,
        WHISPER_BACKEND="openai-api",
        WHISPER_API_BASE_URL="https://asr.example.test/v1",
    )
    _patch_client(monkeypatch, httpx.MockTransport(lambda r: httpx.Response(200, json=["nope"])))
    assert await tts_remote.transcribe(_wav_bytes(), settings) is None
