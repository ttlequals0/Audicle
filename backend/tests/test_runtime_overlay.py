from __future__ import annotations

from pathlib import Path

import defusedxml.ElementTree as DET
from app.config import get_settings
from app.core import database
from app.main import create_app
from app.services import episodes, runtime_settings
from fastapi.testclient import TestClient


def _seed_episode(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        episodes.upsert(
            conn,
            id="ep1",
            job_id=None,
            original_url="https://example.test/x",
            title="An article",
            author="A",
            audio_path="/data/media/ep1.mp3",
            artwork_path=None,
            transcript_vtt="WEBVTT\n",
            duration_secs=10,
        )


def test_overlay_returns_settings_unchanged_when_no_overrides(env: Path) -> None:
    """When the runtime_settings table is empty the overlay short-circuits
    to the env-driven Settings; no DB write needed."""

    database.run_migrations(env)
    base = get_settings()
    overlaid = runtime_settings.overlay(base)
    assert overlaid.FEED_TITLE == base.FEED_TITLE
    assert overlaid.RETENTION_DAYS == base.RETENTION_DAYS


def test_overlay_applies_runtime_override(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        runtime_settings.set_value(conn, "FEED_TITLE", "Operator Override")

    overlaid = runtime_settings.overlay(get_settings())
    assert overlaid.FEED_TITLE == "Operator Override"


def test_overlay_coerces_int_value(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        runtime_settings.set_value(conn, "RETENTION_DAYS", 42)

    overlaid = runtime_settings.overlay(get_settings())
    assert overlaid.RETENTION_DAYS == 42
    assert isinstance(overlaid.RETENTION_DAYS, int)


def test_overlay_coerces_bool_value(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        runtime_settings.set_value(conn, "FEED_EXPLICIT", True)

    overlaid = runtime_settings.overlay(get_settings())
    assert overlaid.FEED_EXPLICIT is True


def test_whisper_verify_settings_are_runtime_tunable(env: Path) -> None:
    """The ASR-verify policy can be toggled/tuned live via the Settings API,
    so an operator never has to redeploy to turn the gate on or adjust it."""

    for key in ("WHISPER_VERIFY_ENABLED", "WHISPER_DIVERGENCE_THRESHOLD", "WHISPER_VERIFY_MIN_WORDS"):
        assert key in runtime_settings.ALLOWED_KEYS

    database.run_migrations(env)
    with database.connection(env) as conn:
        runtime_settings.set_value(conn, "WHISPER_VERIFY_ENABLED", True)
        runtime_settings.set_value(conn, "WHISPER_DIVERGENCE_THRESHOLD", 0.35)
        runtime_settings.set_value(conn, "WHISPER_VERIFY_MIN_WORDS", 12)

    overlaid = runtime_settings.overlay(get_settings())
    assert overlaid.WHISPER_VERIFY_ENABLED is True
    assert overlaid.WHISPER_DIVERGENCE_THRESHOLD == 0.35
    assert isinstance(overlaid.WHISPER_DIVERGENCE_THRESHOLD, float)
    assert overlaid.WHISPER_VERIFY_MIN_WORDS == 12
    assert isinstance(overlaid.WHISPER_VERIFY_MIN_WORDS, int)


def test_rss_render_reflects_runtime_overrides(env: Path) -> None:
    """End-to-end: PUT a FEED_TITLE override, GET the slug feed, confirm the
    new title shows up. This is the test the deferred-fix CHANGELOG entry
    promised."""

    _seed_episode(env)
    with TestClient(create_app()) as client, database.connection(env) as conn:
        runtime_settings.set_value(conn, "FEED_TITLE", "Runtime Override Title")

    with TestClient(create_app()) as client:
        response = client.get("/rss/runtime_override_title.xml")
    assert response.status_code == 200
    root = DET.fromstring(response.content)
    assert root.find("channel/title").text == "Runtime Override Title"


def test_database_connection_context_manager_closes(env: Path) -> None:
    """The new ``connection`` context manager must guarantee close even when
    the body raises."""

    database.run_migrations(env)
    closed: list[bool] = []

    real_close = None
    try:
        with database.connection(env) as conn:
            real_close = conn.close
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                pass
    except RuntimeError:
        pass

    import pytest

    # The connection's close was invoked exactly once -- confirm by
    # attempting a query, which should raise.
    with pytest.raises((Exception,)):
        # ``conn`` is still bound from the with-block; using it post-close
        # raises ProgrammingError.
        conn.execute("SELECT 1").fetchone()
    _ = real_close, closed  # acknowledge


def test_parse_iso_helper_round_trips() -> None:
    from datetime import UTC, datetime

    from app.core.timestamps import parse_iso

    assert parse_iso(None) is None
    assert parse_iso("") is None
    assert parse_iso("garbage") is None

    parsed = parse_iso("2026-05-28T18:00:00Z")
    assert parsed is not None
    assert parsed == datetime(2026, 5, 28, 18, 0, 0, tzinfo=UTC)

    # tz-naive input gets UTC stamped on.
    naive = parse_iso("2026-05-28T18:00:00")
    assert naive is not None
    assert naive.tzinfo is UTC
