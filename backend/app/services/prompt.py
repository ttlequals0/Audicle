"""Cleanup prompt file management.

The prompt lives at ``backend/app/prompts/script.txt`` (bind-mounted), is
editable via ``PUT /api/v1/prompt``, and is re-read on every job.

Both the bind-mount edit path and the API edit path write to the same file
via ``os.replace`` after a temp-file write so concurrent reads always see a
consistent prompt (the pipeline's ``cleanup`` stage open-reads this file at
the start of each job).
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.services.atomic_write import write_bytes_atomic

logger = logging.getLogger("app.services.prompt")


class PromptTooLargeError(Exception):
    """Raised on save when the proposed prompt exceeds the configured cap.

    Subclasses :class:`Exception` (not :class:`ValueError`) so a future broad
    ``except ValueError`` somewhere in the call chain can't silently swallow
    a size-cap signal that callers route to an HTTP 413.
    """


def load(path: Path) -> str:
    """Return the current prompt text. Missing file raises FileNotFoundError so
    the operator notices a misconfigured mount instead of silently running with
    no rules."""

    return path.read_text(encoding="utf-8")


def save(path: Path, content: str, *, max_bytes: int) -> None:
    """Atomic write with a size guard.

    Encoded length (not character count) is what the cap applies to, since
    multi-byte characters can push a "small" prompt over the limit.
    """

    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        raise PromptTooLargeError(
            f"prompt is {len(encoded)} bytes, exceeds MAX_PROMPT_LENGTH_BYTES={max_bytes}"
        )
    write_bytes_atomic(path, encoded, prefix=".script-")
