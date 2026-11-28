"""IP address helpers shared by the API layer."""

from __future__ import annotations

import ipaddress


def parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse an IP with an IPv4-mapped IPv6 form unwrapped, or None if invalid."""

    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def normalize_ip(value: str) -> str | None:
    """Return the IP with an IPv4-mapped IPv6 form unwrapped, or None if invalid."""

    ip = parse_ip(value)
    return None if ip is None else str(ip)
