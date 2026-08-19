"""Shared httpx client + SSRF guard TTL cache.

Both TTS backends issued a fresh ``httpx.AsyncClient`` per HTTP call, paying a
TCP+TLS handshake per chunk; the remote backend's SSRF guard also re-resolved
DNS for the same operator-configured host on every chunk. This covers the
process-wide client singleton and the guard's per-host TTL cache.
"""

from __future__ import annotations

import httpx
import pytest
from app.services import tts, tts_remote


def test_shared_client_returns_same_instance() -> None:
    a = tts.shared_client()
    b = tts.shared_client()
    assert a is b
    assert isinstance(a, httpx.AsyncClient)


async def test_shared_client_recreated_after_close() -> None:
    a = tts.shared_client()
    await a.aclose()
    b = tts.shared_client()
    assert b is not a
    assert not b.is_closed


async def test_guard_caches_verdict_per_host(monkeypatch: pytest.MonkeyPatch) -> None:
    tts_remote._guard_cache.clear()
    calls = {"n": 0}

    async def fake_resolve(_host: str) -> str:
        calls["n"] += 1
        return "203.0.113.1"

    monkeypatch.setattr(tts_remote.ssrf, "resolve_public_host", fake_resolve)

    await tts_remote._guard("http://example.com/x")
    await tts_remote._guard("http://example.com/x")
    assert calls["n"] == 1


async def test_guard_cache_clear_forces_revalidation(monkeypatch: pytest.MonkeyPatch) -> None:
    tts_remote._guard_cache.clear()
    calls = {"n": 0}

    async def fake_resolve(_host: str) -> str:
        calls["n"] += 1
        return "203.0.113.1"

    monkeypatch.setattr(tts_remote.ssrf, "resolve_public_host", fake_resolve)

    await tts_remote._guard("http://example.com/x")
    tts_remote._guard_cache.clear()
    await tts_remote._guard("http://example.com/x")
    assert calls["n"] == 2


async def test_guard_failure_does_not_populate_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    tts_remote._guard_cache.clear()

    async def fake_resolve(_host: str) -> str:
        raise tts_remote.ssrf.BlockedHostError("example.com", "non_public_address")

    monkeypatch.setattr(tts_remote.ssrf, "resolve_public_host", fake_resolve)

    with pytest.raises(tts_remote.ssrf.BlockedHostError):
        await tts_remote._guard("http://example.com/x")
    assert "example.com" not in tts_remote._guard_cache
