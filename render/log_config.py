"""Structured logging for the sidecar.

JSON for Loki, text for local. Mirrors the main app's formatter so a render line
and an app line carry the same fields, and so ``extra={...}`` survives to the log
aggregator: with plain logging every structured field is dropped and a line like
"registration gate result" says nothing about whether the gate opened.

Standalone (rather than imported from the backend) because ``render/`` is its own
package with its own lockfile and image.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from functools import cache
from typing import Any

# Attributes stdlib logging puts on every record. Anything else came from
# extra={...} and is surfaced as its own field.
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
# uvicorn installs its own handlers; folding them into the root logger keeps the
# access lines in the same format as ours.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
# The main app polls these every ~30 s; their 200s were the sidecar's only output.
_POLL_PATHS = frozenset({"/health/live", "/runtime-config"})


@cache
def _hostname() -> str:
    return os.environ.get("HOSTNAME") or socket.gethostname()


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_") and value is not None
    }


class _QuietPollFilter(logging.Filter):
    """Drop successful access records for the health and config polls."""

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access args: (client, method, path, http_version, status_code)
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        path = str(args[2]).partition("?")[0]
        status = args[4]
        return not (path in _POLL_PATHS and isinstance(status, int) and 200 <= status < 300)


class JSONFormatter(logging.Formatter):
    """Emit each record as a single JSON line."""

    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03dZ"

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": "render",
            "hostname": _hostname(),
            "pid": os.getpid(),
        }
        for key, value in _extras(record).items():
            payload.setdefault(key, value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable single line for local runs."""

    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname:<5} {record.name} {record.getMessage()}"
        extras = _extras(record)
        if extras:
            base = f"{base} [{' '.join(f'{k}={v}' for k, v in extras.items())}]"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def setup_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Reconfigure the root logger from LOG_LEVEL/LOG_FORMAT. Idempotent."""

    level = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    fmt = fmt or os.environ.get("LOG_FORMAT") or "json"

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JSONFormatter() if fmt == "json" else TextFormatter())
    root.addHandler(handler)
    try:
        root.setLevel(level)
    except ValueError:
        root.setLevel(logging.INFO)

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.filters = [f for f in access.filters if not isinstance(f, _QuietPollFilter)]
    access.addFilter(_QuietPollFilter())


def apply_logging(level: str, fmt: str) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    formatter = JSONFormatter() if fmt == "json" else TextFormatter()
    for handler in root.handlers:
        handler.setFormatter(formatter)
