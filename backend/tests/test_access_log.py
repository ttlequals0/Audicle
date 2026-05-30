from __future__ import annotations

import logging
from pathlib import Path

import pytest
from app.core import database
from app.main import create_app
from fastapi.testclient import TestClient


def _access_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == "http_access"]


def test_access_log_emits_one_record_per_request(
    env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database.run_migrations(env)
    # No `with` (don't run lifespan): bootstrap's setup_logging would drop
    # caplog's root handler. The access middleware is wired in create_app.
    client = TestClient(create_app())
    with caplog.at_level(logging.INFO, logger="app.access"):
        client.get("/health/live")

    records = _access_records(caplog)
    assert records, "expected an http_access record"
    rec = records[-1]
    assert rec.method == "GET"
    assert rec.path == "/health/live"
    assert rec.status == 200
    assert isinstance(rec.duration_ms, float)
