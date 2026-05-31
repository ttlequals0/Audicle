"""Pronunciation corrections.

Word-level overrides applied between LLM cleanup and chunking. The user
dictionary is stored in the ``settings`` table (DB-backed), editable via
``PUT /api/v1/corrections``, and re-read on every job. ``load(path)`` remains
for the one-time migration that imports a legacy on-disk ``pronunciation.json``.

Substitution mechanics:

- Whole-word matches via lookarounds that treat letters/digits/underscores
  *and hyphens* as word characters: ``kubectl`` does not match inside
  ``kubectl-helper``, and keys ending in non-word symbols like ``C++`` still
  match next to whitespace (plain ``\\b`` refuses ``+`` next to space).
- Case-sensitive (operators add multiple casings when they want every form
  corrected the same way).
- Longest-key-first: a single regex with all keys joined by ``|`` and ordered
  by descending length so ``kubectl`` is replaced before ``kube``.
- Keys are ``re.escape``-d before compile so operators can write ``C++`` or
  ``node.js`` without learning regex.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services import settings_store

logger = logging.getLogger("app.services.corrections")

# Per build plan validation rules. Surfaced as constants so tests can reference
# the same limits as the validator without re-stating them.
MAX_KEY_CHARS = 100
MAX_VALUE_CHARS = 200
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")  # all control chars except \t


@dataclass(frozen=True)
class ValidationFailure:
    key: str
    reason: str


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    failures: list[ValidationFailure] = field(default_factory=list)


def validate(dictionary: Any, *, max_entries: int) -> ValidationResult:
    """Verify ``dictionary`` matches the build plan's PUT schema.

    Returns the failures list rather than raising so the API handler can
    return all of them in one 400 response.
    """

    if not isinstance(dictionary, dict):
        return ValidationResult(
            ok=False,
            failures=[ValidationFailure(key="<root>", reason="must be a JSON object")],
        )

    if len(dictionary) > max_entries:
        return ValidationResult(
            ok=False,
            failures=[
                ValidationFailure(
                    key="<root>",
                    reason=f"too many entries ({len(dictionary)} > {max_entries})",
                )
            ],
        )

    failures: list[ValidationFailure] = []
    for raw_key, raw_value in dictionary.items():
        for fail in _validate_entry(raw_key, raw_value):
            failures.append(fail)
    return ValidationResult(ok=not failures, failures=failures)


def _validate_entry(raw_key: Any, raw_value: Any):
    if not isinstance(raw_key, str):
        yield ValidationFailure(key=str(raw_key), reason="key must be a string")
        return
    if not raw_key:
        yield ValidationFailure(key=raw_key, reason="key must be non-empty")
    if len(raw_key) > MAX_KEY_CHARS:
        yield ValidationFailure(key=raw_key, reason=f"key length > {MAX_KEY_CHARS}")
    if raw_key != raw_key.strip():
        yield ValidationFailure(key=raw_key, reason="key has leading or trailing whitespace")
    if not isinstance(raw_value, str):
        yield ValidationFailure(key=raw_key, reason="value must be a string")
        return
    if not raw_value:
        yield ValidationFailure(key=raw_key, reason="value must be non-empty")
    if len(raw_value) > MAX_VALUE_CHARS:
        yield ValidationFailure(key=raw_key, reason=f"value length > {MAX_VALUE_CHARS}")
    if _CONTROL_CHAR_RE.search(raw_value):
        yield ValidationFailure(key=raw_key, reason="value contains control characters")


def apply(text: str, dictionary: dict[str, str]) -> str:
    """Replace every whole-word match in ``text`` per the dictionary.

    Single pass via regex alternation -- a longer key's replacement can never
    be re-matched by a shorter key because the longer alternative wins at the
    same starting position.
    """

    if not dictionary:
        return text
    sorted_keys = sorted(dictionary, key=len, reverse=True)
    # Use lookarounds that treat letters, digits, underscores AND hyphens as
    # word characters so:
    # - ``kubectl`` doesn't match inside ``kubectl-helper`` (hyphen counts).
    # - Keys ending in non-word symbols like ``C++`` still match correctly
    #   (``\b`` would refuse to match ``+`` next to whitespace).
    pattern = re.compile(
        r"(?<![\w-])(?:" + "|".join(re.escape(k) for k in sorted_keys) + r")(?![\w-])"
    )
    return pattern.sub(lambda match: dictionary[match.group(0)], text)


def load(path: Path) -> dict[str, str]:
    """Load the pronunciation dictionary from disk.

    Missing file is treated as an empty dictionary so a fresh deploy with no
    overrides doesn't fail the pipeline. Malformed JSON raises so the operator
    fixes the file rather than silently losing corrections.
    """

    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ValueError(f"pronunciation.json must be a JSON object, got {type(body).__name__}")
    return body


def load_user_dict(conn: sqlite3.Connection) -> dict[str, str]:
    """Load the operator pronunciation dictionary from the DB (empty if unset)."""

    raw = settings_store.get(conn, settings_store.PRONUNCIATION_KEY)
    if not raw or not raw.strip():
        return {}
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ValueError(f"stored corrections must be a JSON object, got {type(body).__name__}")
    return body


def save_user_dict(conn: sqlite3.Connection, dictionary: dict[str, str]) -> None:
    """Persist the operator pronunciation dictionary to the DB as JSON."""

    settings_store.set_(
        conn,
        settings_store.PRONUNCIATION_KEY,
        json.dumps(dictionary, ensure_ascii=False),
    )
