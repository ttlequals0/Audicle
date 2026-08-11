"""The sidecar's log lines have to carry their structured fields.

Plain logging dropped every ``extra={...}`` key, which made "registration gate
result" unreadable in Loki: the line never said whether the gate opened.
"""

from __future__ import annotations

import json
import logging

from log_config import JSONFormatter, TextFormatter, setup_logging


def _record(**extra) -> logging.LogRecord:
    record = logging.LogRecord("render.camoufox", logging.INFO, __file__, 10, "gate result", (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_line_keeps_the_extra_fields() -> None:
    line = json.loads(JSONFormatter().format(_record(event="render_registration_done", unlocked=False)))

    assert line["event"] == "render_registration_done"
    assert line["unlocked"] is False
    assert line["message"] == "gate result"
    assert line["logger"] == "render.camoufox"
    assert line["service"] == "render"
    assert line["timestamp"].endswith("Z")


def test_json_line_carries_the_traceback() -> None:
    try:
        raise ValueError("submit failed")
    except ValueError:
        import sys

        record = _record()
        record.exc_info = sys.exc_info()

    assert "ValueError: submit failed" in json.loads(JSONFormatter().format(record))["exception"]


def test_text_format_shows_the_extras_too() -> None:
    assert "unlocked=True" in TextFormatter().format(_record(unlocked=True))


def test_setup_installs_one_handler_and_folds_in_uvicorn() -> None:
    setup_logging(level="INFO", fmt="json")
    setup_logging(level="INFO", fmt="json")  # idempotent: no handler pile-up

    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JSONFormatter)
    access = logging.getLogger("uvicorn.access")
    assert access.handlers == [] and access.propagate


def test_bad_level_falls_back_to_info() -> None:
    setup_logging(level="not-a-level", fmt="text")

    assert logging.getLogger().level == logging.INFO
