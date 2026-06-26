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


def validate_lexicon(dictionary: Any, *, max_entries: int) -> ValidationResult:
    """Validate the object-schema correction payload.

    Each value is either a plain string (shorthand for ``{spoken: value}``) or an
    object ``{mode?, spoken, case_sensitive?}``. ``spoken`` is required and
    follows the same length/control-char rules as the flat schema; ``mode`` must
    be one of spell/word/override; ``case_sensitive`` an optional bool.
    """

    from app.services import pronounce_convert  # local import avoids a cycle

    if not isinstance(dictionary, dict):
        return ValidationResult(
            ok=False, failures=[ValidationFailure(key="<root>", reason="must be a JSON object")]
        )
    if len(dictionary) > max_entries:
        return ValidationResult(
            ok=False,
            failures=[
                ValidationFailure(
                    key="<root>", reason=f"too many entries ({len(dictionary)} > {max_entries})"
                )
            ],
        )
    failures: list[ValidationFailure] = []
    for raw_key, raw_value in dictionary.items():
        failures.extend(_validate_key(raw_key))
        spoken = raw_value if isinstance(raw_value, str) else None
        if isinstance(raw_value, dict):
            spoken = raw_value.get("spoken")
            mode = raw_value.get("mode")
            if mode is not None and mode not in pronounce_convert.MODES:
                failures.append(ValidationFailure(key=str(raw_key), reason=f"invalid mode {mode!r}"))
            cs = raw_value.get("case_sensitive")
            if cs is not None and not isinstance(cs, bool):
                failures.append(
                    ValidationFailure(key=str(raw_key), reason="case_sensitive must be a bool")
                )
        elif not isinstance(raw_value, str):
            failures.append(
                ValidationFailure(key=str(raw_key), reason="value must be a string or object")
            )
            continue
        failures.extend(_validate_value(str(raw_key), spoken))
    return ValidationResult(ok=not failures, failures=failures)


def _validate_key(raw_key: Any) -> list[ValidationFailure]:
    out: list[ValidationFailure] = []
    if not isinstance(raw_key, str):
        return [ValidationFailure(key=str(raw_key), reason="key must be a string")]
    if not raw_key:
        out.append(ValidationFailure(key=raw_key, reason="key must be non-empty"))
    if len(raw_key) > MAX_KEY_CHARS:
        out.append(ValidationFailure(key=raw_key, reason=f"key length > {MAX_KEY_CHARS}"))
    if raw_key != raw_key.strip():
        out.append(ValidationFailure(key=raw_key, reason="key has leading or trailing whitespace"))
    return out


def _validate_value(key: str, value: Any) -> list[ValidationFailure]:
    out: list[ValidationFailure] = []
    if not isinstance(value, str):
        return [ValidationFailure(key=key, reason="spoken must be a string")]
    if not value:
        out.append(ValidationFailure(key=key, reason="spoken must be non-empty"))
    if len(value) > MAX_VALUE_CHARS:
        out.append(ValidationFailure(key=key, reason=f"spoken length > {MAX_VALUE_CHARS}"))
    if _CONTROL_CHAR_RE.search(value):
        out.append(ValidationFailure(key=key, reason="spoken contains control characters"))
    return out


def apply(text: str, dictionary: dict[str, str], *, case_sensitive: bool = True) -> str:
    """Replace every whole-word match in ``text`` per the dictionary.

    Single pass via regex alternation -- a longer key's replacement can never
    be re-matched by a shorter key because the longer alternative wins at the
    same starting position.

    ``case_sensitive=False`` folds case so an entry keyed ``404 media`` still hits
    ``404 Media`` in the article (the lexicon marks override entries case-insensitive
    by default). The matched text then differs from the key, so the lookup folds too;
    on a fold collision the longest key wins (sorted longest-first).
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
        r"(?<![\w-])(?:" + "|".join(re.escape(k) for k in sorted_keys) + r")(?![\w-])",
        0 if case_sensitive else re.IGNORECASE,
    )
    if case_sensitive:
        return pattern.sub(lambda match: dictionary[match.group(0)], text)
    folded: dict[str, str] = {}
    for key in sorted_keys:  # longest first: it wins any fold collision
        folded.setdefault(key.casefold(), dictionary[key])
    return pattern.sub(lambda match: folded[match.group(0).casefold()], text)


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
