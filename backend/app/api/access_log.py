"""Structured HTTP access logging.

One log record per request (``event=http_access``) with method, path, status,
duration, and client IP, plus a per-request ``request_id`` stamped onto every
log emitted while the request is in flight (so a request's access line and its
downstream logs share an id). Replaces uvicorn's plain-text access log, which
is silenced in ``utils.logging``.
"""

from __future__ import annotations

import logging
import time
import uuid

from starlette.types import ASGIApp, Receive, Scope, Send

from app.utils.logging import request_id_ctx

logger = logging.getLogger("app.access")

# Header a reverse proxy sets with the real client IP. Logged as-is for
# observability; not used for any security decision.
_FORWARDED_FOR = b"x-forwarded-for"


class AccessLogMiddleware:
    """Pure-ASGI middleware so the request_id contextvar is set for the entire
    request lifecycle (a function middleware runs in a different task than the
    endpoint and wouldn't propagate the contextvar to handler logs)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex[:12]
        token = request_id_ctx.set(request_id)
        start = time.monotonic()
        # 0 = no response started (client disconnect / crash before start), so
        # it doesn't masquerade as a real 500 in error-rate dashboards.
        status_holder = {"code": 0}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            client = scope.get("client")
            # Scan the header list for the one header we log rather than
            # materializing a full dict on every request.
            forwarded = next(
                (v for k, v in scope.get("headers") or () if k == _FORWARDED_FOR), None
            )
            qs = scope.get("query_string")
            query = qs.decode("latin-1") if qs else None
            logger.info(
                "http_access",
                extra={
                    "event": "http_access",
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "query": query,
                    "status": status_holder["code"],
                    "duration_ms": duration_ms,
                    "client": client[0] if client else None,
                    "forwarded_for": forwarded.decode("latin-1") if forwarded else None,
                },
            )
            request_id_ctx.reset(token)
