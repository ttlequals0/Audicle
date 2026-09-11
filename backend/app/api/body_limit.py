from __future__ import annotations

import json
import sqlite3
import tempfile

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import envelope
from app.config import Settings
from app.core import database

_FIXED_LIMITS = {
    "/api/v1/chime": 11 * 1024 * 1024,
}


class UploadBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    def _limit(self, path: str) -> int | None:
        path = path.rstrip("/") or "/"
        if path == "/api/v1/upload":
            megabytes = self.settings.UPLOAD_MAX_MB
            try:
                with database.connection(self.settings.DATA_DIR) as conn:
                    row = conn.execute(
                        "SELECT value FROM runtime_settings WHERE key = 'UPLOAD_MAX_MB'"
                    ).fetchone()
                if row is not None:
                    megabytes = int(json.loads(row["value"]))
            except (OSError, sqlite3.Error, TypeError, ValueError):
                pass
            return megabytes * 1024 * 1024 + 1024 * 1024
        if path.startswith("/api/v1/reference/"):
            return 6 * 1024 * 1024
        return _FIXED_LIMITS.get(path)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        limit = self._limit(scope.get("path", ""))
        if limit is None:
            await self.app(scope, receive, send)
            return
        content_length = next(
            (value for name, value in scope.get("headers", ()) if name == b"content-length"), None
        )
        try:
            advertised = int(content_length) if content_length is not None else None
        except ValueError:
            advertised = limit + 1
        if advertised is not None and (advertised < 0 or advertised > limit):
            await envelope(status=413, error="request body too large")(scope, receive, send)
            return
        received = 0
        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as body:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                received += len(chunk)
                if received > limit:
                    await envelope(status=413, error="request body too large")(scope, receive, send)
                    return
                body.write(chunk)
                if not message.get("more_body", False):
                    break
            body.seek(0)

            async def replay_receive() -> Message:
                chunk = body.read(64 * 1024)
                return {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": bool(chunk) and body.tell() < received,
                }

            await self.app(scope, replay_receive, send)
