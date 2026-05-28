from __future__ import annotations

import logging
from pathlib import Path

from app.core import database
from app.startup import bootstrap


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
