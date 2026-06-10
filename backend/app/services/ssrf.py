"""Shared SSRF guard for outbound fetches.

Resolves a hostname and refuses private/loopback/link-local/multicast/reserved
targets so a caller-supplied URL cannot make the service reach internal
addresses (RFC 1918 ranges, cloud metadata endpoints, other compose services).

The artwork download path additionally pins the connection to the resolved IP to
close the DNS-rebinding TOCTOU (see ``pin_url_to_ip``). The extraction pipeline's
fetches (Firecrawl, FlareSolverr) are performed by a separate container, so the
app can't pin those connections; there the realistic guard is host validation
before the URL is handed off, via ``assert_url_public``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class BlockedHostError(RuntimeError):
    def __init__(self, host: str, reason: str, *, blocked: bool = True) -> None:
        super().__init__(f"host {host!r} blocked: {reason}")
        self.host = host
        self.reason = reason
        # True when the host resolved to a non-public address -- a real SSRF hit,
        # permanent. False when resolution itself failed (DNS error, no records,
        # empty host): the caller can't conclude the target is internal, so it
        # should fall through to the normal fetch path rather than hard-reject.
        self.blocked = blocked


def pin_url_to_ip(url: str, ip: str) -> str:
    """Return ``url`` rewritten so the host component is ``ip`` (literal),
    preserving scheme, port, path, query, and fragment. IPv6 literals are
    bracketed."""

    parts = urlsplit(url)
    netloc_host = f"[{ip}]" if ":" in ip else ip
    netloc = f"{netloc_host}:{parts.port}" if parts.port else netloc_host
    if parts.username or parts.password:
        # Preserve credentials in case (operator-set basic auth).
        userinfo = parts.username or ""
        if parts.password:
            userinfo += f":{parts.password}"
        netloc = f"{userinfo}@{netloc}"
    return parts._replace(netloc=netloc).geturl()


async def resolve_public_host(host: str) -> str:
    """SSRF guard: resolve ``host`` and refuse private/loopback/link-local/
    multicast/reserved addresses. Returns the resolved IP literal so the
    caller can pin the connection to that IP -- closing the DNS-rebinding
    TOCTOU where the validation lookup gets a public IP and httpx's
    subsequent lookup gets a private one.

    The returned IP literal is substituted into the URL via ``pin_url_to_ip``;
    httpx still sends the original ``Host:`` header (preserved via
    ``Request.headers``) so the upstream sees the operator-controlled name
    and TLS SNI works correctly.
    """

    if not host:
        raise BlockedHostError("", "empty hostname", blocked=False)
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedHostError(host, f"dns_resolution_failed: {exc}", blocked=False) from exc
    if not infos:
        raise BlockedHostError(host, "dns_no_records", blocked=False)
    public_ip: str | None = None
    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            raise BlockedHostError(host, f"invalid_ip_{addr}") from None
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise BlockedHostError(host, f"non_public_address_{ip}")
        if public_ip is None:
            public_ip = str(ip)
    assert public_ip is not None  # we'd have raised above
    return public_ip


async def assert_url_public(url: str) -> None:
    """Raise ``BlockedHostError`` if ``url``'s host resolves to a non-public
    address. Used before handing a caller-supplied URL to an out-of-process
    fetcher (Firecrawl, FlareSolverr) where the app can't pin the connection."""

    await resolve_public_host(urlsplit(url).hostname or "")
