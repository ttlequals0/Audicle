"""Tests for the proxy-aware client-IP resolver (app.api.deps.client_ip)."""

from __future__ import annotations

from types import SimpleNamespace

from app.api.deps import client_ip
from starlette.datastructures import Headers


def _request(xff: str | None = None, peer: str | None = "9.9.9.9") -> SimpleNamespace:
    headers = Headers({"x-forwarded-for": xff} if xff else {})
    client = SimpleNamespace(host=peer) if peer is not None else None
    return SimpleNamespace(headers=headers, client=client)


def _settings(trust: bool = False, hops: int = 1) -> SimpleNamespace:
    return SimpleNamespace(TRUST_PROXY_HEADERS=trust, TRUSTED_PROXY_HOPS=hops)


def test_uses_socket_peer_when_proxy_headers_untrusted() -> None:
    req = _request(xff="1.1.1.1, 2.2.2.2")
    assert client_ip(req, _settings(trust=False)) == "9.9.9.9"


def test_takes_rightmost_xff_entry_at_one_hop() -> None:
    req = _request(xff="6.6.6.6, 7.7.7.7, 8.8.8.8")
    assert client_ip(req, _settings(trust=True, hops=1)) == "8.8.8.8"


def test_takes_second_from_right_at_two_hops() -> None:
    req = _request(xff="6.6.6.6, 7.7.7.7, 8.8.8.8")
    assert client_ip(req, _settings(trust=True, hops=2)) == "7.7.7.7"


def test_ignores_spoofed_leftmost_entry() -> None:
    # Attacker prepends 6.6.6.6; the proxy appends the real client 7.7.7.7.
    req = _request(xff="6.6.6.6, 7.7.7.7")
    assert client_ip(req, _settings(trust=True, hops=1)) == "7.7.7.7"


def test_falls_back_to_peer_when_xff_absent() -> None:
    req = _request(xff=None)
    assert client_ip(req, _settings(trust=True, hops=1)) == "9.9.9.9"


def test_falls_back_to_peer_on_unparseable_candidate() -> None:
    req = _request(xff="not-an-ip")
    assert client_ip(req, _settings(trust=True, hops=1)) == "9.9.9.9"


def test_falls_back_to_peer_when_hops_exceed_chain() -> None:
    req = _request(xff="7.7.7.7")
    assert client_ip(req, _settings(trust=True, hops=2)) == "9.9.9.9"


def test_returns_unknown_when_no_peer_and_no_header() -> None:
    req = _request(xff=None, peer=None)
    assert client_ip(req, _settings(trust=True, hops=1)) == "unknown"
