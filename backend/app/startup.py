"""Shared startup helpers used by both the FastAPI lifespan and the worker.

Keeping these in one place means future startup additions (metrics init, schema
version logging, sentry, ...) only have to be wired up once.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
from pathlib import Path

from app.config import Settings
from app.core import database
from app.utils.logging import setup_logging
from app.version import __version__

_APP_DIR = Path(__file__).resolve().parent


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
    _seed_defaults(settings, logger)


def _seed_defaults(settings: Settings, logger: logging.Logger) -> None:
    """Copy packaged defaults into their writable runtime locations when missing.

    A bind-mount over ``/app/app/prompts`` or ``/app/app/corrections`` shadows
    the shipped files, so an empty mount would otherwise hide the default prompt
    and pronunciation corrections; the default podcast artwork lands in
    ``DATA_DIR/media`` so the feed's channel image resolves until the operator
    sets ``FEED_ARTWORK_URL``. Idempotent: an existing target (even one the
    operator edited or emptied) is left alone.
    """

    defaults = _APP_DIR / "defaults"
    for src, dst in (
        (defaults / "script.txt", _APP_DIR / "prompts" / "script.txt"),
        (defaults / "summary.txt", _APP_DIR / "prompts" / "summary.txt"),
        (defaults / "pronunciation.json", _APP_DIR / "corrections" / "pronunciation.json"),
        (_APP_DIR / "assets" / "default-artwork.jpg", settings.DATA_DIR / "media" / "default.jpg"),
    ):
        _seed_if_missing(src, dst, logger)


def _seed_if_missing(src: Path, dst: Path, logger: logging.Logger) -> None:
    if dst.exists() or not src.exists():
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        logger.info("Seeded default", extra={"event": "seed_default", "path": str(dst)})
    except OSError:
        logger.warning(
            "Could not seed default file",
            extra={"event": "seed_default_failed", "path": str(dst)},
            exc_info=True,
        )
