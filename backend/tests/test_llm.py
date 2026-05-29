from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from app.config import get_settings
from app.services import llm


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _capture_transport(*, response: httpx.Response):
    """A MockTransport that records the request it sees so the test can assert
    the wire format the LLM client sent."""

    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return response

    return httpx.MockTransport(handler), captured


def _openai_ok(text: str = "cleaned") -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": text}}]}
        ).encode(),
        headers={"content-type": "application/json"},
    )


def _anthropic_ok(text: str = "cleaned") -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps({"content": [{"type": "text", "text": text}]}).encode(),
        headers={"content-type": "application/json"},
    )


# --- openai-compatible ------------------------------------------------------


async def test_openai_compatible_sends_chat_completions_payload(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, captured = _capture_transport(response=_openai_ok("cleaned text"))
    _patch_async_client(monkeypatch, transport)

    result = await llm.generate(
        "system rules", "article body", get_settings(), temperature=0.3, max_tokens=42
    )

    assert result == "cleaned text"
    req = captured["request"]
    assert req.url.path.endswith("/chat/completions")
    body = json.loads(req.content)
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"] == "system rules"
    assert body["messages"][1]["content"] == "article body"
    assert body["temperature"] == 0.3
    assert body["max_tokens"] == 42
    assert req.headers.get("authorization") == "Bearer test-key"


async def test_openai_compatible_5xx_raises_provider_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(500, text="boom"))
    _patch_async_client(monkeypatch, transport)

    with pytest.raises(llm.LLMProviderError):
        await llm.generate("s", "u", get_settings())


async def test_openai_compatible_4xx_raises_request_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(400, text="bad"))
    _patch_async_client(monkeypatch, transport)

    with pytest.raises(llm.LLMRequestError):
        await llm.generate("s", "u", get_settings())


async def test_openai_compatible_timeout_raises_timeout_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(_request):
        raise httpx.ReadTimeout("slow")

    _patch_async_client(monkeypatch, httpx.MockTransport(_raise))

    with pytest.raises(llm.LLMTimeoutError):
        await llm.generate("s", "u", get_settings())


async def test_openai_compatible_non_json_body_raises_request_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = httpx.Response(200, text="<html>not json</html>")
    _patch_async_client(monkeypatch, httpx.MockTransport(lambda _r: response))

    with pytest.raises(llm.LLMRequestError, match="non-JSON"):
        await llm.generate("s", "u", get_settings())


async def test_openai_compatible_unexpected_shape_raises_request_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = httpx.Response(200, json={"weird": True})
    _patch_async_client(monkeypatch, httpx.MockTransport(lambda _r: response))

    with pytest.raises(llm.LLMRequestError):
        await llm.generate("s", "u", get_settings())


# --- anthropic --------------------------------------------------------------


async def test_anthropic_sends_messages_payload(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    get_settings.cache_clear()

    transport, captured = _capture_transport(response=_anthropic_ok("clean"))
    _patch_async_client(monkeypatch, transport)

    result = await llm.generate("system rules", "article", get_settings())

    assert result == "clean"
    req = captured["request"]
    assert req.url.host == "api.anthropic.com"
    assert req.headers.get("x-api-key") == "ant-key"
    assert req.headers.get("anthropic-version") == llm.ANTHROPIC_VERSION
    body = json.loads(req.content)
    assert body["system"] == "system rules"
    assert body["messages"] == [{"role": "user", "content": "article"}]


async def test_anthropic_requires_api_key(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    get_settings.cache_clear()
    settings = get_settings()

    # Mutate after-the-fact: settings are frozen as a pydantic model, but the
    # function reads the key directly so a None override surfaces clearly.
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    with pytest.raises(llm.LLMRequestError, match="ANTHROPIC_API_KEY"):
        await llm.generate("s", "u", settings)


async def test_anthropic_non_text_content_block_raises(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    get_settings.cache_clear()

    response = httpx.Response(200, json={"content": [{"type": "tool_use", "input": {}}]})
    _patch_async_client(monkeypatch, httpx.MockTransport(lambda _r: response))

    with pytest.raises(llm.LLMRequestError, match="no text blocks"):
        await llm.generate("s", "u", get_settings())


async def test_unknown_provider_raises_request_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bypass pydantic Literal validation by editing the loaded settings.
    settings = get_settings()
    monkeypatch.setattr(settings, "LLM_PROVIDER", "made-up")
    with pytest.raises(llm.LLMRequestError, match="Unknown LLM_PROVIDER"):
        await llm.generate("s", "u", settings)


async def test_anthropic_url_path_and_version_are_correct(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock the hardcoded Anthropic endpoint shape so a regression to /v1/complete
    or a missing anthropic-version header fails the test instead of production."""

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    get_settings.cache_clear()

    transport, captured = _capture_transport(response=_anthropic_ok("text"))
    _patch_async_client(monkeypatch, transport)

    await llm.generate("s", "u", get_settings())

    req = captured["request"]
    assert req.url.path == "/v1/messages", req.url
    assert req.headers["anthropic-version"] == "2023-06-01"


async def test_openai_compatible_null_content_raises_typed_request_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some providers return content=null when tool_calls would be issued;
    we must classify this as LLMRequestError (not propagate a TypeError on
    len()) so the pipeline reports a clean failure."""

    response = httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": []}}]},
    )
    _patch_async_client(monkeypatch, httpx.MockTransport(lambda _r: response))

    with pytest.raises(llm.LLMRequestError, match="non-string content"):
        await llm.generate("s", "u", get_settings())


async def test_anthropic_picks_text_block_among_thinking_and_tool_use(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anthropic returns multiple content blocks when extended thinking is on;
    we must skip non-text blocks and concatenate text blocks rather than
    crashing on the first non-text block at index 0."""

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    get_settings.cache_clear()

    response = httpx.Response(
        200,
        json={
            "content": [
                {"type": "thinking", "thinking": "internal reasoning"},
                {"type": "text", "text": "part one. "},
                {"type": "tool_use", "input": {}},
                {"type": "text", "text": "part two."},
            ]
        },
    )
    _patch_async_client(monkeypatch, httpx.MockTransport(lambda _r: response))

    result = await llm.generate("s", "u", get_settings())
    assert result == "part one. part two."


async def test_openai_compatible_empty_base_url_raises_request_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unconfigured install (no OPENAI_BASE_URL) must fail the stage with a
    clear error instead of letting httpx raise on a relative URL."""

    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    get_settings.cache_clear()
    with pytest.raises(llm.LLMRequestError, match="OPENAI_BASE_URL"):
        await llm.generate("s", "u", get_settings())
