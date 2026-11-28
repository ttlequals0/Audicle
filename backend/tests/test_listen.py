from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn
from app import listen
from app.listen import listen_socket


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


def test_serve_sets_tcp_nodelay_on_accepted_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guards against --fd, which makes uvicorn label the socket AF_UNIX and asyncio skip NODELAY.
    servers: list[uvicorn.Server] = []

    class CapturingServer(uvicorn.Server):
        def __init__(self, config: uvicorn.Config) -> None:
            super().__init__(config)
            servers.append(self)

    async def asgi(scope, receive, send) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"ok"})

    monkeypatch.setattr(listen.uvicorn, "Server", CapturingServer)
    sock, _ = listen_socket(0)
    port = sock.getsockname()[1]
    config = uvicorn.Config(asgi, lifespan="off", log_level="warning")
    thread = threading.Thread(target=listen.serve, args=(config, sock))
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not (servers and servers[0].started) and time.monotonic() < deadline:
            time.sleep(0.02)
        with socket.create_connection(("127.0.0.1", port), timeout=2) as c:
            c.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            assert b"200 OK" in c.recv(4096)
            (protocol,) = servers[0].server_state.connections
            conn = protocol.transport.get_extra_info("socket")
            assert conn.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) == 1
    finally:
        servers[0].should_exit = True
        thread.join(5)
