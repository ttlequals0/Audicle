from __future__ import annotations

import json
import logging

import pytest
from app.utils.logging import (
    episode_id_ctx,
    job_id_ctx,
    setup_logging,
    stage_ctx,
    status_ctx,
)


def test_json_formatter_emits_required_fields(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(level="DEBUG", fmt="json")
    logging.getLogger("test.json").info("hello", extra={"event": "test_event"})
    output = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(output)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.json"
    assert payload["message"] == "hello"
    assert payload["event"] == "test_event"
    assert payload["service"] == "audicle"
    assert "timestamp" in payload
    assert "hostname" in payload
    assert "pid" in payload


def test_json_formatter_passes_through_arbitrary_extras(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Earlier versions used a whitelist that silently dropped arbitrary
    extra= keys (error, path, count, ...). The denylist approach must surface
    every non-standard attribute."""

    setup_logging(level="DEBUG", fmt="json")
    logging.getLogger("test.passthrough").info(
        "msg",
        extra={
            "event": "evt",
            "error": "boom",
            "path": "/tmp/x",
            "count": 5,
            "attempt": 2,
        },
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["error"] == "boom"
    assert payload["path"] == "/tmp/x"
    assert payload["count"] == 5
    assert payload["attempt"] == 2


def test_json_formatter_timestamp_has_milliseconds_and_z_suffix(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging(level="DEBUG", fmt="json")
    logging.getLogger("test.ts").info("m")
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    # ISO-ish + .NNN + Z
    assert payload["timestamp"].endswith("Z"), payload["timestamp"]
    assert "." in payload["timestamp"], payload["timestamp"]


def test_context_filter_injects_job_id(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(level="DEBUG", fmt="json")
    token = job_id_ctx.set("job-1234")
    try:
        logging.getLogger("test.ctx").info("with-context")
    finally:
        job_id_ctx.reset(token)
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["job_id"] == "job-1234"


def test_context_filter_injects_episode_id(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(level="DEBUG", fmt="json")
    token = episode_id_ctx.set("ep-abc")
    try:
        logging.getLogger("test.ep").info("with-episode")
    finally:
        episode_id_ctx.reset(token)
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["episode_id"] == "ep-abc"


def test_context_filter_injects_stage_and_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging(level="DEBUG", fmt="json")
    s = stage_ctx.set("tts")
    t = status_ctx.set("processing")
    try:
        logging.getLogger("test.stage").info("mid-stage")
    finally:
        stage_ctx.reset(s)
        status_ctx.reset(t)
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["stage"] == "tts"
    assert payload["status"] == "processing"


def test_text_formatter_is_human_readable(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(level="INFO", fmt="text")
    logging.getLogger("test.txt").info("plain message", extra={"event": "evt"})
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert "INFO" in line
    assert "plain message" in line
    assert "event=evt" in line


def test_setup_logging_is_idempotent_and_does_not_leak_handlers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging(level="INFO", fmt="json")
    setup_logging(level="INFO", fmt="json")
    assert len(logging.getLogger().handlers) == 1

    logging.getLogger("test.once").info("single")
    lines = [ln for ln in capsys.readouterr().out.strip().splitlines() if "single" in ln]
    assert len(lines) == 1
