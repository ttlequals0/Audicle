from __future__ import annotations

import logging
from pathlib import Path

from app.core import database
from app.startup import _is_local_base_url, _warn_if_open_mode, bootstrap


def _settings_with(data_dir: Path):
    from app.config import get_settings

    get_settings.cache_clear()
    return get_settings()


def test_bootstrap_runs_migrations_and_configures_logging(env: Path) -> None:
    bootstrap(_settings_with(env), process_label="test")
    # Logging is wired up.
    assert len(logging.getLogger().handlers) == 1
    # Schema is in place.
    conn = database.connect(database.db_path(env))
    try:
        names = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert {"jobs", "episodes", "schema_migrations"}.issubset(names)


def test_is_local_base_url() -> None:
    assert _is_local_base_url("http://localhost:8000")
    assert _is_local_base_url("http://127.0.0.1:8000")
    assert not _is_local_base_url("https://audicle.example.com")


def test_warns_in_convenience_mode(env: Path, caplog) -> None:
    database.run_migrations(env)
    logger = logging.getLogger("app.startup")
    with caplog.at_level(logging.WARNING, logger="app.startup"):
        _warn_if_open_mode(_settings_with(env), logger)
    events = {getattr(record, "event", "") for record in caplog.records}
    assert {"convenience_mode_active", "convenience_mode_exposed"} & events


def test_silent_when_password_set(env: Path, caplog) -> None:
    from app.services import auth

    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        auth.set_password(conn, "correct-horse")
    finally:
        conn.close()
    logger = logging.getLogger("app.startup")
    with caplog.at_level(logging.WARNING, logger="app.startup"):
        _warn_if_open_mode(_settings_with(env), logger)
    events = {getattr(record, "event", "") for record in caplog.records}
    assert not ({"convenience_mode_active", "convenience_mode_exposed"} & events)


def test_bootstrap_is_safe_to_call_twice(env: Path) -> None:
    """Both the FastAPI lifespan (web) and the queue worker invoke bootstrap;
    the second call must not fail or duplicate handlers, and the migration runner
    must report a no-op."""

    bootstrap(_settings_with(env), process_label="web")
    bootstrap(_settings_with(env), process_label="worker")
    assert len(logging.getLogger().handlers) == 1
    # A third explicit run still finds nothing pending.
    applied = database.run_migrations(env)
    assert applied == []
