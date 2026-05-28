from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from app.config import get_settings
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
    assert body == {"text": "hello", "episode_id": "ep-1", "chunk_index": 3}


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
