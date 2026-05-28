"""Shared filesystem path helpers.

Single source of truth for the on-disk layout under ``DATA_DIR``. Stages and
Phase 7's finalize use these to avoid hard-coding ``settings.DATA_DIR /
"media"`` in multiple places and drifting when the layout changes.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings


def media_dir(settings: Settings) -> Path:
    return settings.DATA_DIR / "media"
