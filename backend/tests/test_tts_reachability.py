"""Reachability tests specific to the TTS wrapper grace period."""

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


def _seq(*responses) -> httpx.MockTransport:
    iterator = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            value = next(iterator)
        except StopIteration as exc:
            raise AssertionError("TTS reachability made more calls than expected") from exc
        if isinstance(value, Exception):
            raise value
        return value

    return httpx.MockTransport(handler)


async def test_check_tts_ok_first_try(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_async_client(
        monkeypatch,
        _seq(
            httpx.Response(200, json={"ok": True, "model_loaded": True, "reference_loaded": True})
        ),
    )
    result = await reachability.check_tts(get_settings())
    assert result.ok is True
    assert "model_loaded=true" in result.detail
    assert "reference_loaded=True" in result.detail


async def test_check_tts_polls_until_model_loaded(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cold-start: wrapper responds 200 but model isn't loaded yet for a couple
    of probes, then finally reports model_loaded=true."""

    _patch_async_client(
        monkeypatch,
        _seq(
            httpx.Response(
                200, json={"ok": False, "model_loaded": False, "reference_loaded": False}
            ),
            httpx.Response(
                200, json={"ok": False, "model_loaded": False, "reference_loaded": True}
            ),
            httpx.Response(200, json={"ok": True, "model_loaded": True, "reference_loaded": True}),
        ),
    )
    result = await reachability.check_tts(get_settings())
    assert result.ok is True


async def test_check_tts_reports_failure_after_grace_expires(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # env fixture sets TTS_REACHABILITY_GRACE_SECONDS=0.5 + probe=0.5
    # so this test wraps up in well under a second.
    def _raise(_request):
        raise httpx.ConnectError("no service")

    _patch_async_client(monkeypatch, httpx.MockTransport(_raise))

    result = await reachability.check_tts(get_settings())
    assert result.ok is False
    assert "grace period" in result.detail


async def test_check_tts_reports_failure_when_model_never_loads(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_async_client(
        monkeypatch,
        httpx.MockTransport(
            lambda _r: httpx.Response(
                200, json={"ok": False, "model_loaded": False, "reference_loaded": False}
            )
        ),
    )
    result = await reachability.check_tts(get_settings())
    assert result.ok is False
    assert "model_loaded=false" in result.detail
