"""Shared filesystem path helpers.

Single source of truth for the on-disk layout under ``DATA_DIR``. Stages and
The finalize stage uses these to avoid hard-coding ``settings.DATA_DIR /
"media"`` in multiple places and drifting when the layout changes.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings


def media_dir(settings: Settings) -> Path:
    return settings.DATA_DIR / "media"


def file_size_or_zero(path_str: str | None) -> int:
    """Best-effort byte size of ``path_str``; 0 when missing/unreadable.

    Used for the RSS enclosure ``length`` and the episodes API
    ``audio_size_bytes`` so both agree and neither 500s on a stale row.
    """

    if not path_str:
        return 0
    try:
        return Path(path_str).stat().st_size
    except OSError:
        return 0
