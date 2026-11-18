"""Structured JSON logging for the tts-wrapper.

Mirrors the backend's log shape (timestamp/level/logger/message/event + extras)
so both services parse identically in Loki. The wrapper previously used
``logging.basicConfig``, which rendered records as plain ``INFO:tts.main:...``
text -- the ``extra={...}`` fields were dropped and multi-line tracebacks broke
Loki's JSON parser. Routing uvicorn's loggers through the same handler makes
startup errors and per-request events structured too.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import socket
import sys
import threading
from contextlib import contextmanager
from functools import cache
from typing import Any

# tqdm reads this at import, so set it before chatterbox/whisper pull tqdm in; the
# bars would otherwise write carriage-return noise to the container log per chunk.
os.environ.setdefault("TQDM_DISABLE", "1")

LIB_LOGGER = "tts.engine.lib"
_LINE_BREAKS = re.compile(r"[\r\n]+")

_SERVICE = "tts-wrapper"

# ``uvicorn.error`` is uvicorn's general lifecycle logger, not an error channel:
# "Application startup complete" and "Uvicorn running on ..." arrive on it at
# INFO. The name leaks into the log line, and a shipper that derives a level by
# scanning the raw text (Grafana Alloy does) matches the word "error" and labels
# every one of those records as an error. The whole wrapper stream was
# level=error in Loki as a result, which makes an error-rate panel useless.
# Reporting the honest name fixes the label without touching the shipper.
_LOGGER_ALIASES = {"uvicorn.error": "uvicorn"}


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
            "logger": _LOGGER_ALIASES.get(record.name, record.name),
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


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        details = " ".join(
            f"{key}={value}"
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_") and value is not None
        )
        line = f"{record.levelname:<5} {record.name} {record.getMessage()}"
        if details:
            line = f"{line} [{details}]"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


class _LineLogger(io.TextIOBase):
    """File-like sink that logs each non-empty line at DEBUG."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._pending = ""
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        with self._lock:
            *lines, self._pending = _LINE_BREAKS.split(self._pending + text)
        for line in lines:
            if line.strip():
                self._logger.debug(line.strip())
        return len(text)

    def flush(self) -> None:
        with self._lock:
            line, self._pending = self._pending.strip(), ""
        if line:
            self._logger.debug(line)


_capture_lock = threading.Lock()
_capture_depth = 0
_capture_saved: Any = None


@contextmanager
def capture_stdout(logger_name: str = LIB_LOGGER):
    """Route library ``print()`` output to a DEBUG logger.

    ``sys.stdout`` is process-global, so overlapping captures (load vs inference
    threads) share one sink and the last one out restores it. The JSON handler
    holds the original stream object, so log records still reach the real stdout.
    """

    global _capture_depth, _capture_saved
    with _capture_lock:
        if _capture_depth == 0:
            _capture_saved = sys.stdout
            sys.stdout = _LineLogger(logging.getLogger(logger_name))
        _capture_depth += 1
    try:
        yield
    finally:
        with _capture_lock:
            _capture_depth -= 1
            if _capture_depth == 0:
                sys.stdout.flush()
                sys.stdout = _capture_saved


def apply_logging(level: str, fmt: str) -> None:
    resolved = getattr(logging, level.upper(), None)
    if not isinstance(resolved, int) or fmt not in {"json", "text"}:
        raise ValueError("invalid logging configuration")
    root = logging.getLogger()
    root.setLevel(resolved)
    formatter = JSONFormatter() if fmt == "json" else TextFormatter()
    for handler in root.handlers:
        handler.setLevel(resolved)
        handler.setFormatter(formatter)


def setup_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Install the JSON handler on the root logger and route uvicorn through it.

    Idempotent: clears existing handlers first so a reconfigure (or test
    re-import) doesn't double-log.
    """

    resolved = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    format_name = (fmt or os.environ.get("LOG_FORMAT") or "json").lower()

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JSONFormatter() if format_name == "json" else TextFormatter())
    root.addHandler(handler)
    # Library warnings (e.g. the diffusers LoRA FutureWarning) become py.warnings records.
    logging.captureWarnings(True)
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
