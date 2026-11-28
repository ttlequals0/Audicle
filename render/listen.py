"""Open the listening socket and exec uvicorn on it (`--fd`), dual-stack when possible.

Usage: python -I listen.py NAME PORT UVICORN_ARGS...

uvicorn would bind "::" itself, but asyncio sets IPV6_V6ONLY on every AF_INET6 socket it creates
(single worker), which silently drops IPv4. Creating the socket here keeps that option under
our control. This file is duplicated in backend/ and tts-wrapper/ (separate Docker build contexts).
"""

from __future__ import annotations

import os
import socket
import sys


def listen_socket(port: int) -> tuple[socket.socket, str]:
    """Return a bound socket and its address: dual-stack "::" if possible, else "0.0.0.0"."""

    sock = None
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind(("::", port))
        return sock, "::"
    except OSError:
        if sock is not None:
            sock.close()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    return sock, "0.0.0.0"


def main(argv: list[str]) -> None:
    name, port, *uvicorn_args = argv
    sock, host = listen_socket(int(port))
    sock.set_inheritable(True)
    print(f"{name}: listening on {host}", file=sys.stderr, flush=True)
    os.execvp("uvicorn", ["uvicorn", *uvicorn_args, "--fd", str(sock.fileno())])


if __name__ == "__main__":
    main(sys.argv[1:])
