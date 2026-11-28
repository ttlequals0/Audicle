"""IP address helpers shared by the API layer."""

from __future__ import annotations

import ipaddress


def normalize_ip(value: str) -> str | None:
    """Return the IP with an IPv4-mapped IPv6 form unwrapped, or None if invalid."""

    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return str(ip.ipv4_mapped)
    return str(ip)
