"""Consistent error envelope.

All 4xx/5xx responses use ``{error, status, details?}``. 5xx responses log
the underlying exception with traceback but never leak internal detail to the
client.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.api.errors")


def envelope(*, status: int, error: str, details: dict[str, Any] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": error, "status": status}
    if details is not None:
        body["details"] = details
    return JSONResponse(status_code=status, content=body)


def register(app: FastAPI) -> None:
    """Install the three handlers that cover every error path."""

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # FastAPI's HTTPException already carries a status + detail. If detail
        # is a dict, treat it as our envelope details; otherwise use the string
        # as the error message.
        details: dict[str, Any] | None = None
        if isinstance(exc.detail, dict):
            error = exc.detail.get("error", "HTTP error")
            details = exc.detail.get("details")
        else:
            error = str(exc.detail) if exc.detail else "HTTP error"
        return envelope(status=exc.status_code, error=error, details=details)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # exc.errors() can carry non-JSON-safe ctx values (Pattern, Enum,
        # exception instances) depending on the validator. jsonable_encoder
        # coerces those into primitives so json.dumps doesn't fall through to
        # the catch-all 500 handler.
        return envelope(
            status=400,
            error="Validation failed",
            details={"errors": jsonable_encoder(exc.errors())},
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled exception",
            extra={
                "event": "unhandled_exception",
                "path": str(request.url.path),
                "method": request.method,
            },
        )
        # Never leak details on 500.
        return envelope(status=500, error="Internal server error")
