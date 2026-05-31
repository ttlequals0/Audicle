"""Cleanup + summary prompts, stored in the DB with packaged defaults.

The shipped default for each prompt is a read-only file baked into the image at
``app/defaults/{script,summary}.txt``. An operator edit is stored as a row in
the ``settings`` table and wins over the default; clearing it restores the
default. The pipeline resolves the effective prompt per job, so edits take
effect on the next job with no restart -- and nothing is written to disk.
"""

from __future__ import annotations

import logging
import sqlite3
from functools import cache
from pathlib import Path
from typing import Literal

from app.services import settings_store

logger = logging.getLogger("app.services.prompt")

PromptKind = Literal["cleanup", "summary"]

# Per kind: (settings-table key for the override, packaged default filename).
_PROMPTS: dict[PromptKind, tuple[str, str]] = {
    "cleanup": (settings_store.CLEANUP_PROMPT_KEY, "script.txt"),
    "summary": (settings_store.SUMMARY_PROMPT_KEY, "summary.txt"),
}


class PromptTooLargeError(Exception):
    """Raised on save when the proposed prompt exceeds the configured cap.

    Subclasses :class:`Exception` (not :class:`ValueError`) so a future broad
    ``except ValueError`` somewhere in the call chain can't silently swallow
    a size-cap signal that callers route to an HTTP 413.
    """


def _defaults_dir() -> Path:
    return Path(__file__).parent.parent / "defaults"


@cache
def default_text(kind: PromptKind) -> str:
    """The packaged default prompt text (read-only, baked into the image).

    Cached: the default files are immutable image content, so the per-job read
    is wasted after the first.
    """

    return (_defaults_dir() / _PROMPTS[kind][1]).read_text(encoding="utf-8")


def _override(conn: sqlite3.Connection, kind: PromptKind) -> str | None:
    """The stored operator override, or None when unset/blank (use the default)."""

    value = settings_store.get(conn, _PROMPTS[kind][0])
    return value if value is not None and value.strip() else None


def load_effective(conn: sqlite3.Connection, kind: PromptKind) -> str:
    """Return the operator override if one is stored, else the packaged default."""

    return _override(conn, kind) or default_text(kind)


def is_default(conn: sqlite3.Connection, kind: PromptKind) -> bool:
    """True when no (non-blank) override is stored -- the default is in effect."""

    return _override(conn, kind) is None


def load_with_flag(conn: sqlite3.Connection, kind: PromptKind) -> tuple[str, bool]:
    """Effective text plus whether it is the default, from a single DB read."""

    override = _override(conn, kind)
    if override is not None:
        return override, False
    return default_text(kind), True


def save_override(
    conn: sqlite3.Connection, kind: PromptKind, content: str, *, max_bytes: int
) -> None:
    """Store an operator override. Encoded length (not char count) is capped so a
    multi-byte string can't slip past the limit."""

    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        raise PromptTooLargeError(
            f"prompt is {len(encoded)} bytes, exceeds MAX_PROMPT_LENGTH_BYTES={max_bytes}"
        )
    settings_store.set_(conn, _PROMPTS[kind][0], content)


def reset(conn: sqlite3.Connection, kind: PromptKind) -> None:
    """Drop the override so the packaged default takes over again."""

    settings_store.delete(conn, _PROMPTS[kind][0])
