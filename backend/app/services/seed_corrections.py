"""Built-in (seed) pronunciation corrections.

A project-maintained baseline shipped in-repo as a CSV and applied *beneath* the
user's own corrections (the user dictionary wins on key collision). Unlike the
user dictionary it is read-only: hidden from the Settings editor, exposed for
inspection via ``GET /api/v1/corrections/seed``, and never written into the
user-editable ``pronunciation.json``.

Not every row is applied. Two classes are stored and exposed but excluded from
the pipeline:

- Annotated homographs whose input carries a parenthesized qualifier
  (``read (present)``) -- context-dependent, not matchable by whole-word
  substitution. A future contextual layer will handle them.
- ALL-CAPS acronyms whose replacement is the spelled-out letters (``API`` ->
  ``A P I``): the LLM cleanup stage already spells these out (to dotted form)
  before corrections run, so the row would never match the cleaned text.
  Pronounce-as-word acronyms (``RAM`` -> ``ram``) and expansions (``TL;DR`` ->
  ``too long didn't read``) stay applicable.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app.services import corrections

logger = logging.getLogger("app.services.seed_corrections")

_FIELDS = ("category", "input_text", "replacement_text", "notes")
_ANNOTATION_RE = re.compile(r"\([^)]*\)")
_ALLCAPS_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{2,}$")
_ACRONYM_CATEGORIES = frozenset({"Tech Acronym", "General Acronym"})


@dataclass(frozen=True)
class SeedEntry:
    category: str
    input_text: str
    replacement_text: str
    notes: str
    applicable: bool  # True -> merged into the pipeline correction dictionary
    match_key: str | None  # input_text when applicable, else None


def seed_path() -> Path:
    return Path(__file__).parent.parent / "defaults" / "tts_correction_list.csv"


def _is_spelled_out(replacement: str) -> bool:
    """True when the replacement is letter-by-letter spaced spelling (``A P I``)."""

    tokens = replacement.split(" ")
    return len(tokens) > 1 and all(len(tok) == 1 for tok in tokens)


def _match_key(category: str, input_text: str, replacement_text: str) -> str | None:
    """Return the key an applicable row matches on, or None when excluded."""

    # Context-dependent annotated rows (homographs) can't be whole-word matched.
    if _ANNOTATION_RE.search(input_text):
        return None
    # Spelled-out ALL-CAPS acronyms duplicate what LLM cleanup already produces.
    if (
        category in _ACRONYM_CATEGORIES
        and _ALLCAPS_TOKEN_RE.match(input_text)
        and _is_spelled_out(replacement_text)
    ):
        return None
    # Must pass the same per-entry rules the user dictionary enforces.
    result = corrections.validate({input_text: replacement_text}, max_entries=1)
    if not result.ok:
        logger.warning(
            "Seed correction row failed validation; not applying",
            extra={
                "event": "seed_row_invalid",
                "input": input_text,
                "reasons": [f.reason for f in result.failures],
            },
        )
        return None
    return input_text


def load_seed(path: Path) -> list[SeedEntry]:
    """Parse the bundled seed CSV into typed rows.

    Missing file -> empty list (the pipeline runs on the user dictionary alone).
    A malformed CSV (wrong/missing columns) raises so a bad ship is caught in
    tests rather than silently dropping the whole seed layer.
    """

    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or set(_FIELDS) - set(reader.fieldnames):
            raise ValueError(
                f"seed corrections CSV must have columns {_FIELDS}, got {reader.fieldnames}"
            )
        entries: list[SeedEntry] = []
        for raw in reader:
            category = (raw.get("category") or "").strip()
            input_text = (raw.get("input_text") or "").strip()
            replacement_text = (raw.get("replacement_text") or "").strip()
            notes = (raw.get("notes") or "").strip()
            key = _match_key(category, input_text, replacement_text)
            entries.append(
                SeedEntry(
                    category=category,
                    input_text=input_text,
                    replacement_text=replacement_text,
                    notes=notes,
                    applicable=key is not None,
                    match_key=key,
                )
            )
    return entries


def applicable_dict(entries: list[SeedEntry]) -> dict[str, str]:
    """``{match_key: replacement_text}`` for applicable rows; first row wins on dup."""

    result: dict[str, str] = {}
    for entry in entries:
        if entry.match_key is None:
            continue
        if entry.match_key in result:
            logger.warning(
                "Duplicate seed correction key; keeping first",
                extra={"event": "seed_dup_key", "key": entry.match_key},
            )
            continue
        result[entry.match_key] = entry.replacement_text
    return result
