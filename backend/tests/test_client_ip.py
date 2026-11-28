"""Tests for the proxy-aware client-IP resolver (app.api.deps.client_ip)."""

from __future__ import annotations

from types import SimpleNamespace

from app.api.deps import client_ip, limiter_key
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


def test_unwraps_ipv4_mapped_socket_peer() -> None:
    req = _request(xff=None, peer="::ffff:203.0.113.7")
    assert client_ip(req, _settings(trust=False)) == "203.0.113.7"


def test_real_ipv6_socket_peer_keeps_full_address_and_keys_by_slash_64() -> None:
    req = _request(xff=None, peer="2001:db8:1:2::1")
    assert client_ip(req, _settings(trust=False)) == "2001:db8:1:2::1"
    assert limiter_key(req, _settings(trust=False)) == "2001:db8:1:2::/64"


def test_same_slash_64_shares_key_and_other_differs() -> None:
    a = limiter_key(_request(peer="2001:db8:1:2::1"), _settings())
    b = limiter_key(_request(peer="2001:db8:1:2:ffff::9"), _settings())
    c = limiter_key(_request(peer="2001:db8:1:3::1"), _settings())
    assert a == b
    assert a != c


def test_unwraps_ipv4_mapped_xff_entry() -> None:
    req = _request(xff="6.6.6.6, ::ffff:203.0.113.7")
    assert client_ip(req, _settings(trust=True, hops=1)) == "203.0.113.7"


def test_real_ipv6_xff_entry_keeps_full_address_and_keys_by_slash_64() -> None:
    req = _request(xff="6.6.6.6, 2001:db8:1:2::abcd")
    assert client_ip(req, _settings(trust=True, hops=1)) == "2001:db8:1:2::abcd"
    assert limiter_key(req, _settings(trust=True, hops=1)) == "2001:db8:1:2::/64"


def test_limiter_key_leaves_ipv4_mapped_and_unknown_unchanged() -> None:
    assert limiter_key(_request(peer="9.9.9.9"), _settings()) == "9.9.9.9"
    assert limiter_key(_request(peer="::ffff:203.0.113.7"), _settings()) == "203.0.113.7"
    assert limiter_key(_request(peer=None), _settings()) == "unknown"


def test_mapped_peer_falls_back_when_xff_invalid() -> None:
    req = _request(xff="not-an-ip", peer="::ffff:9.9.9.9")
    assert client_ip(req, _settings(trust=True, hops=1)) == "9.9.9.9"
