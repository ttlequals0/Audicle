from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest
from app.config import RUNTIME_SETTING_BOUNDS, Settings, get_settings
from app.services import tts


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _capture_transport(*, response: httpx.Response):
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return response

    return httpx.MockTransport(handler), captured


def _ok_generate(*, wav_path: str = "/data/media/abc_chunk_0.wav") -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps(
            {"wav_path": wav_path, "duration_secs": 12.3, "sample_rate": 24000}
        ).encode(),
        headers={"content-type": "application/json"},
    )


# --- generate_chunk --------------------------------------------------------


async def test_generate_chunk_sends_expected_payload(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, captured = _capture_transport(response=_ok_generate())
    _patch_async_client(monkeypatch, transport)

    result = await tts.generate_chunk("hello", "ep-1", 3, get_settings())

    assert result.wav_path == "/data/media/abc_chunk_0.wav"
    assert result.duration_secs == 12.3
    assert result.sample_rate == 24000
    req = captured["request"]
    assert req.url.path == "/generate"
    body = json.loads(req.content)
    # The generation knobs ride on every call (0.44.0), sourced from settings;
    # with no override the seed is the configured baseline.
    assert body == {
        "text": "hello",
        "episode_id": "ep-1",
        "chunk_index": 3,
        "verify": False,
        "temperature": 0.5,
        "repetition_penalty": 1.2,
        "top_p": 0.95,
        "top_k": 1000,
        "max_chars": 300,
        "seed": 1234,
    }
    # No transcript field in the response => None on the result.
    assert result.transcript is None


async def test_generate_chunk_seed_override_replaces_baseline(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, captured = _capture_transport(response=_ok_generate())
    _patch_async_client(monkeypatch, transport)

    await tts.generate_chunk("hello", "ep-1", 3, get_settings(), seed=999)

    body = json.loads(captured["request"].content)
    assert body["seed"] == 999


# Backend runtime-setting name -> wrapper GenerationParams / bounds field name.
_KNOB_NAME_MAP = {
    "CHATTERBOX_TEMPERATURE": "temperature",
    "CHATTERBOX_REPETITION_PENALTY": "repetition_penalty",
    "CHATTERBOX_TOP_P": "top_p",
    "CHATTERBOX_TOP_K": "top_k",
    "CHATTERBOX_SEED": "seed",
    "CHATTERBOX_MAX_CHARS": "max_chars",
}


def _load_wrapper_engine():
    """Import tts-wrapper/engine.py (stdlib-only at import time; torch and
    numpy imports are deferred) so cross-service tables can be pinned."""

    engine_path = Path(__file__).resolve().parents[2] / "tts-wrapper" / "engine.py"
    spec = importlib.util.spec_from_file_location("wrapper_engine", engine_path)
    assert spec is not None and spec.loader is not None
    wrapper_engine = importlib.util.module_from_spec(spec)
    # dataclasses resolves the module's string annotations via sys.modules.
    sys.modules["wrapper_engine"] = wrapper_engine
    try:
        spec.loader.exec_module(wrapper_engine)
    finally:
        del sys.modules["wrapper_engine"]
    return wrapper_engine


def test_generation_param_defaults_and_bounds_match_wrapper() -> None:
    # The backend Settings defaults/bounds and the wrapper's GenerationParams/
    # GENERATION_BOUNDS are maintained separately, and the backend always sends
    # every field, so a drift on either side would be silent (a widened backend
    # bound would 422 at the wrapper on every chunk). Pin both tables together.
    wrapper_engine = _load_wrapper_engine()
    params = wrapper_engine.GenerationParams()
    fields = Settings.model_fields
    for backend_key, wrapper_key in _KNOB_NAME_MAP.items():
        assert getattr(params, wrapper_key) == fields[backend_key].default, backend_key
        assert (
            RUNTIME_SETTING_BOUNDS[backend_key] == wrapper_engine.GENERATION_BOUNDS[wrapper_key]
        ), backend_key


async def test_generate_chunk_verify_flag_sets_payload_field(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, captured = _capture_transport(response=_ok_generate())
    _patch_async_client(monkeypatch, transport)

    await tts.generate_chunk("hello", "ep-1", 3, get_settings(), verify=True)

    body = json.loads(captured["request"].content)
    assert body["verify"] is True


async def test_generate_chunk_parses_transcript(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = httpx.Response(
        200,
        content=json.dumps(
            {
                "wav_path": "/data/media/abc_chunk_0.wav",
                "duration_secs": 12.3,
                "sample_rate": 24000,
                "transcript": "the spoken words",
            }
        ).encode(),
        headers={"content-type": "application/json"},
    )
    transport, _captured = _capture_transport(response=response)
    _patch_async_client(monkeypatch, transport)

    result = await tts.generate_chunk("hello", "ep-1", 3, get_settings(), verify=True)
    assert result.transcript == "the spoken words"


async def test_generate_chunk_5xx_raises_provider_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_async_client(
        monkeypatch, httpx.MockTransport(lambda _r: httpx.Response(500, text="boom"))
    )
    with pytest.raises(tts.TTSProviderError, match="500"):
        await tts.generate_chunk("hi", "ep", 0, get_settings())


async def test_generate_chunk_4xx_raises_request_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_async_client(
        monkeypatch, httpx.MockTransport(lambda _r: httpx.Response(400, text="bad"))
    )
    with pytest.raises(tts.TTSRequestError, match="400"):
        await tts.generate_chunk("hi", "ep", 0, get_settings())


async def test_generate_chunk_timeout_raises_timeout_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(_request):
        raise httpx.ReadTimeout("slow")

    _patch_async_client(monkeypatch, httpx.MockTransport(_raise))
    with pytest.raises(tts.TTSTimeoutError):
        await tts.generate_chunk("hi", "ep", 0, get_settings())


async def test_generate_chunk_network_error_classified_as_provider_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(_request):
        raise httpx.ConnectError("refused")

    _patch_async_client(monkeypatch, httpx.MockTransport(_raise))
    with pytest.raises(tts.TTSProviderError, match="unreachable"):
        await tts.generate_chunk("hi", "ep", 0, get_settings())


async def test_generate_chunk_non_json_raises_request_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_async_client(
        monkeypatch,
        httpx.MockTransport(lambda _r: httpx.Response(200, text="not json")),
    )
    with pytest.raises(tts.TTSRequestError, match="non-JSON"):
        await tts.generate_chunk("hi", "ep", 0, get_settings())


async def test_generate_chunk_unexpected_shape_raises_request_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_async_client(
        monkeypatch,
        httpx.MockTransport(lambda _r: httpx.Response(200, json={"weird": True})),
    )
    with pytest.raises(tts.TTSRequestError, match="Unexpected"):
        await tts.generate_chunk("hi", "ep", 0, get_settings())


# --- reload ----------------------------------------------------------------


async def test_reload_posts_and_returns_body(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transport, captured = _capture_transport(response=httpx.Response(200, json={"ok": True}))
    _patch_async_client(monkeypatch, transport)

    result = await tts.reload(get_settings())
    assert result == {"ok": True}
    assert captured["request"].url.path == "/reload"
    assert captured["request"].method == "POST"


async def test_reload_5xx_raises_provider_error(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_async_client(
        monkeypatch, httpx.MockTransport(lambda _r: httpx.Response(503, text="oom"))
    )
    with pytest.raises(tts.TTSProviderError, match="503"):
        await tts.reload(get_settings())


# --- generate_chunk_with_retry ---------------------------------------------


async def test_retry_succeeds_after_transient_5xx(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5xx then 200: retry returns the successful result, never raises."""

    monkeypatch.setenv("TTS_RETRY_COUNT", "3")
    from app.config import get_settings as gs

    gs.cache_clear()

    responses = iter([httpx.Response(500, text="boom"), _ok_generate()])

    def handler(_request):
        return next(responses)

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))
    # Strip the exponential wait so the test doesn't sleep.
    import tenacity

    monkeypatch.setattr(tenacity.wait_exponential, "__call__", lambda self, rs: 0)

    result = await tts.generate_chunk_with_retry("hi", "ep", 0, gs())
    assert result.wav_path == "/data/media/abc_chunk_0.wav"


async def test_retry_does_not_retry_on_4xx(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """4xx is non-retryable; first attempt raises and propagates."""

    monkeypatch.setenv("TTS_RETRY_COUNT", "3")
    from app.config import get_settings as gs

    gs.cache_clear()

    attempts = {"n": 0}

    def handler(_request):
        attempts["n"] += 1
        return httpx.Response(400, text="bad")

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(tts.TTSRequestError, match="400"):
        await tts.generate_chunk_with_retry("hi", "ep", 0, gs())
    assert attempts["n"] == 1


async def test_retry_exhausts_then_raises_provider_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TTS_RETRY_COUNT", "3")
    from app.config import get_settings as gs

    gs.cache_clear()

    attempts = {"n": 0}

    def handler(_request):
        attempts["n"] += 1
        return httpx.Response(500, text="still down")

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))
    import tenacity

    monkeypatch.setattr(tenacity.wait_exponential, "__call__", lambda self, rs: 0)

    with pytest.raises(tts.TTSProviderError):
        await tts.generate_chunk_with_retry("hi", "ep", 0, gs())
    assert attempts["n"] == 3
