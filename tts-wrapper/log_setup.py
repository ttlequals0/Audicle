"""Structured JSON logging for the tts-wrapper.

Mirrors the backend's log shape (timestamp/level/logger/message/event + extras)
so both services parse identically in Loki. The wrapper previously used
``logging.basicConfig``, which rendered records as plain ``INFO:tts.main:...``
text -- the ``extra={...}`` fields were dropped and multi-line tracebacks broke
Loki's JSON parser. Routing uvicorn's loggers through the same handler makes
startup errors and per-request events structured too.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from functools import cache
from typing import Any

_SERVICE = "tts-wrapper"


@cache
def _hostname() -> str:
    """Resolved once per process. Lazy (not at import) so a container that sets
    HOSTNAME after Python boots still sees the intended value on the first record."""

    return os.environ.get("HOSTNAME") or socket.gethostname()

# Standard LogRecord attributes; anything else on the record came from extra={}.
_STANDARD_RECORD_ATTRS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName", "color_message",
    }
)


class JSONFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03dZ"

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": _SERVICE,
            "hostname": _hostname(),
            "pid": os.getpid(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key.startswith("_"):
                continue
            if value is None:
                continue
            payload.setdefault(key, value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str | None = None) -> None:
    """Install the JSON handler on the root logger and route uvicorn through it.

    Idempotent: clears existing handlers first so a reconfigure (or test
    re-import) doesn't double-log.
    """

    resolved = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)
    try:
        root.setLevel(resolved)
    except ValueError:
        root.setLevel(logging.INFO)

    # uvicorn installs its own handlers; clear them and let records propagate to
    # root so startup tracebacks and lifecycle lines emit as JSON. Access logs
    # (a /health probe every 30s from both the docker healthcheck and the
    # backend readiness poll) are quieted to keep the real pipeline steps legible.
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = True
    access.setLevel(logging.WARNING)
