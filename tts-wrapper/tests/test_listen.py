from __future__ import annotations

import socket

import pytest

from listen import listen_socket


def _has_ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
    except OSError:
        return False
    return True


def _connects(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as c:
        c.settimeout(2)
        try:
            c.connect((host, port))
        except OSError:
            return False
    return True


def test_listen_socket_is_dual_stack() -> None:
    sock, host = listen_socket(0)
    with sock:
        sock.listen()
        port = sock.getsockname()[1]
        if host != "::":
            pytest.skip("host cannot bind AF_INET6")
        assert sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 0
        assert _connects("127.0.0.1", port)
        if _has_ipv6_loopback():
            assert _connects("::1", port)


def test_listen_socket_falls_back_to_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    real = socket.socket

    def no_v6(family=-1, *args, **kwargs):
        if family == socket.AF_INET6:
            raise OSError("ipv6 disabled")
        return real(family, *args, **kwargs)

    monkeypatch.setattr(socket, "socket", no_v6)
    sock, host = listen_socket(0)
    with sock:
        sock.listen()
        assert host == "0.0.0.0"
        assert sock.family == socket.AF_INET
        assert _connects("127.0.0.1", sock.getsockname()[1])
