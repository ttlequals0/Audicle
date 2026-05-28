"""LLM-specific reachability tests (separate file to avoid bloating test_reachability)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from app.config import get_settings
from app.services import reachability


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def test_llm_openai_compatible_ok(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(200, json={"data": [{"id": "qwen-test"}]})
    )
    _patch_async_client(monkeypatch, transport)

    result = await reachability.check_llm(get_settings())
    assert result.ok is True
    assert "200" in result.detail


async def test_llm_openai_compatible_reports_network_failure(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(_request):
        raise httpx.ConnectError("nope")

    _patch_async_client(monkeypatch, httpx.MockTransport(_raise))

    result = await reachability.check_llm(get_settings())
    assert result.ok is False
    assert "unreachable" in result.detail


async def test_llm_openai_compatible_reports_5xx(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(500, text="boom"))
    _patch_async_client(monkeypatch, transport)

    result = await reachability.check_llm(get_settings())
    assert result.ok is False
    assert "500" in result.detail


async def test_llm_anthropic_no_probe_only_validates_key(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    get_settings.cache_clear()

    # No transport patch -- the anthropic path shouldn't make any HTTP call.
    result = await reachability.check_llm(get_settings())
    assert result.ok is True
    assert "anthropic" in result.detail.lower()


async def test_llm_anthropic_reports_missing_key(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "valid-key-for-settings")
    get_settings.cache_clear()
    settings = get_settings()
    # Strip the key after Settings loaded so we exercise check_llm's own guard.
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)

    result = await reachability.check_llm(settings)
    assert result.ok is False
    assert "ANTHROPIC_API_KEY" in result.detail
