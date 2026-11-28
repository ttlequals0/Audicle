"""Open the listening socket ourselves and serve uvicorn on it, dual-stack when possible.

Run as `python -m app.listen`. With one worker uvicorn would bind "::" itself, but asyncio sets
IPV6_V6ONLY on that socket, which silently drops IPv4 (with more workers uvicorn binds it and
leaves the option at the system default). Setting it explicitly also removes the dependence on
the net.ipv6.bindv6only sysctl. `--fd` is no
alternative: uvicorn labels that socket AF_UNIX, so asyncio skips TCP_NODELAY. So we start
the server in-process with our socket, as `uvicorn.run` does. This file is duplicated in
render/ and tts-wrapper/ (separate Docker build contexts).
"""

from __future__ import annotations

import os
import socket
import sys

import uvicorn
from uvicorn.config import STARTUP_FAILURE
from uvicorn.supervisors import Multiprocess


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


def serve(config: uvicorn.Config, sock: socket.socket) -> None:
    """Serve on `sock` the way uvicorn.run does after it binds."""

    sock.set_inheritable(True)
    server = uvicorn.Server(config)
    try:
        if config.workers > 1:
            Multiprocess(config, sockets=[sock]).run()
        else:
            server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    if config.workers == 1 and not server.started:
        sys.exit(STARTUP_FAILURE)


def main() -> None:
    config = uvicorn.Config("app.main:app", workers=int(os.environ.get("WEB_WORKERS") or "2"))
    sock, host = listen_socket(8000)
    print(f"audicle: listening on {host}", file=sys.stderr, flush=True)
    serve(config, sock)


if __name__ == "__main__":
    main()
