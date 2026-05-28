"""Structured logging.

JSON for Loki, text for local. Context propagation via contextvars (FastAPI-safe).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from contextvars import ContextVar
from functools import cache
from typing import Any

# Context fields that should be stamped onto every log record while set.
job_id_ctx: ContextVar[str | None] = ContextVar("job_id_ctx", default=None)
episode_id_ctx: ContextVar[str | None] = ContextVar("episode_id_ctx", default=None)
stage_ctx: ContextVar[str | None] = ContextVar("stage_ctx", default=None)
status_ctx: ContextVar[str | None] = ContextVar("status_ctx", default=None)

# Constant low-cardinality label every record carries so Loki / Promtail can
# index by it. Set via configure_service() at startup; defaults to "audicle".
_service: str = "audicle"


def configure_service(name: str) -> None:
    """Set the constant 'service' field stamped on every log record."""

    global _service
    _service = name


# Anything stuffed into the LogRecord by stdlib logging itself or by our
# ContextFilter that we don't want surfaced as a "user context" field. Anything
# else passed via extra={...} flows through to the output untouched.
_STANDARD_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "asctime",
        "taskName",
    }
)


@cache
def _hostname() -> str:
    """Resolved once per process at first log emission, then memoized.

    Captured lazily (not at module import) so containers that set HOSTNAME after
    Python boots, or tests that monkeypatch socket.gethostname, still see the
    intended value on the first record.
    """

    return os.environ.get("HOSTNAME") or socket.gethostname()


def _context_payload(record: logging.LogRecord) -> dict[str, Any]:
    """Return every non-standard LogRecord attribute as a dict.

    Anything passed via extra={...} (or stamped by ContextFilter) lands on the
    record as an attribute. We surface all of them and let the formatter emit
    them, rather than maintaining a brittle whitelist that silently drops
    keys the caller intended to log.
    """

    payload: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in _STANDARD_RECORD_ATTRS or key.startswith("_"):
            continue
        if value is None:
            continue
        payload[key] = value
    return payload


_CTX_VARS: tuple[tuple[str, ContextVar[str | None]], ...] = (
    ("job_id", job_id_ctx),
    ("episode_id", episode_id_ctx),
    ("stage", stage_ctx),
    ("status", status_ctx),
)


class ContextFilter(logging.Filter):
    """Pull contextvars onto the LogRecord so the formatter can read them."""

    def filter(self, record: logging.LogRecord) -> bool:
        for attr, var in _CTX_VARS:
            if getattr(record, attr, None) is None:
                value = var.get()
                if value is not None:
                    setattr(record, attr, value)
        return True


class JSONFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03dZ"

    def format(self, record: logging.LogRecord) -> str:
        # Call formatTime() without an explicit datefmt so default_msec_format
        # gets applied; passing datefmt= bypasses the msec-suffix branch in
        # logging.Formatter.formatTime and the trailing ".NNNZ" is lost.
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": _service,
            "hostname": _hostname(),
            "pid": os.getpid(),
        }
        # Caller-supplied extras override defaults only if the caller really
        # passed conflicting keys; in practice the standard keys above aren't
        # used as extra= names.
        for key, value in _context_payload(record).items():
            payload.setdefault(key, value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable single-line format for local dev."""

    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<5} {record.name} {record.getMessage()}"
        ctx = _context_payload(record)
        if ctx:
            extras = " ".join(f"{key}={value}" for key, value in ctx.items())
            base = f"{base} [{extras}]"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


_THIRD_PARTY_QUIET = ("httpx", "httpcore", "uvicorn.access", "asyncio")


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Reconfigure the root logger. Idempotent."""

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JSONFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(ContextFilter())
    root.addHandler(handler)

    try:
        root.setLevel(level.upper())
    except ValueError:
        root.setLevel(logging.INFO)

    for name in _THIRD_PARTY_QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
