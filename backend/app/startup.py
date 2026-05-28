"""Shared startup helpers used by both the FastAPI lifespan and the worker.

Keeping these in one place means future startup additions (metrics init, schema
version logging, sentry, ...) only have to be wired up once.
"""

from __future__ import annotations

import logging
import os
import platform
import socket

from app.config import Settings
from app.core import database
from app.utils.logging import setup_logging
from app.version import __version__


def bootstrap(settings: Settings, *, process_label: str) -> None:
    """Configure logging, log the banner, and apply pending migrations.

    Safe to call from every process in the supervised pair: ``run_migrations``
    serializes itself via the .migration.lock file.
    """

    setup_logging(level=settings.LOG_LEVEL, fmt=settings.LOG_FORMAT)
    logger = logging.getLogger("app.startup")
    logger.info(
        "Audicle starting",
        extra={
            "event": "app_starting",
            "version": __version__,
            "python": platform.python_version(),
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "process_label": process_label,
        },
    )
    applied = database.run_migrations(settings.DATA_DIR)
    logger.info(
        "Migrations complete",
        extra={"event": "migrations_complete", "count": len(applied)},
    )
