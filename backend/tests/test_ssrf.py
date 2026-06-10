"""SSRF guard tests for app.services.ssrf.

Marked ``real_ssrf`` so the autouse public-IP stub in conftest is skipped and the
genuine resolver runs. Hosts are numeric IP literals so getaddrinfo resolves them
locally without a network/DNS round trip.
"""

from __future__ import annotations

import pytest
from app.services import ssrf

pytestmark = pytest.mark.real_ssrf


async def test_resolve_public_host_accepts_public_ip() -> None:
    assert await ssrf.resolve_public_host("8.8.8.8") == "8.8.8.8"


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",  # loopback
        "10.0.0.1",  # private
        "192.168.1.1",  # private
        "172.16.0.1",  # private
        "169.254.0.1",  # link-local
        "::1",  # IPv6 loopback
        "0.0.0.0",  # unspecified
    ],
)
async def test_resolve_public_host_blocks_non_public(host: str) -> None:
    with pytest.raises(ssrf.BlockedHostError):
        await ssrf.resolve_public_host(host)


async def test_resolve_public_host_blocks_empty() -> None:
    with pytest.raises(ssrf.BlockedHostError) as excinfo:
        await ssrf.resolve_public_host("")
    assert excinfo.value.reason == "empty hostname"
    assert excinfo.value.blocked is False


async def test_private_address_sets_blocked_true() -> None:
    with pytest.raises(ssrf.BlockedHostError) as excinfo:
        await ssrf.resolve_public_host("192.168.0.1")
    assert excinfo.value.blocked is True


async def test_dns_failure_sets_blocked_false() -> None:
    # A name that can't resolve (RFC 2606 .invalid) is a resolution failure, not a
    # confirmed-internal target, so blocked is False and callers fall through.
    with pytest.raises(ssrf.BlockedHostError) as excinfo:
        await ssrf.resolve_public_host("nonexistent.invalid")
    assert excinfo.value.blocked is False


async def test_assert_url_public_blocks_private_url() -> None:
    with pytest.raises(ssrf.BlockedHostError):
        await ssrf.assert_url_public("http://192.168.0.1/secret")


async def test_assert_url_public_blocks_loopback_url() -> None:
    with pytest.raises(ssrf.BlockedHostError):
        await ssrf.assert_url_public("http://127.0.0.1:8000/admin")


async def test_assert_url_public_allows_public_url() -> None:
    await ssrf.assert_url_public("http://8.8.8.8/article")  # no raise


def test_pin_url_to_ip_rewrites_host_preserving_path_and_port() -> None:
    assert (
        ssrf.pin_url_to_ip("https://example.com:8443/a?b=1#c", "203.0.113.7")
        == "https://203.0.113.7:8443/a?b=1#c"
    )


def test_pin_url_to_ip_brackets_ipv6() -> None:
    assert ssrf.pin_url_to_ip("https://example.com/x", "2001:db8::1") == "https://[2001:db8::1]/x"
