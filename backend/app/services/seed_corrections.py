"""Built-in (seed) pronunciation corrections.

A project-maintained baseline shipped in-repo as a CSV and applied *beneath* the
user's own corrections (the user dictionary wins on key collision). Unlike the
user dictionary it is read-only: hidden from the Settings editor, exposed for
inspection via ``GET /api/v1/corrections/seed``, and never written into the
user-editable ``pronunciation.json``.

Every row is imported into the ``lexicon`` table (:func:`build_lexicon_rows`) and
shown to the LLM pronunciation pass (:func:`format_reference`), which applies
context-dependent entries (annotated homographs such as ``read (present)``) by
meaning -- something a whole-word substitution can't do.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

_FIELDS = ("category", "input_text", "replacement_text", "notes")


@dataclass(frozen=True)
class SeedEntry:
    category: str
    input_text: str
    replacement_text: str
    notes: str


def seed_path() -> Path:
    return Path(__file__).parent.parent / "defaults" / "tts_correction_list.csv"


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
            input_text = (raw.get("input_text") or "").strip()
            replacement_text = (raw.get("replacement_text") or "").strip()
            entries.append(
                SeedEntry(
                    category=(raw.get("category") or "").strip(),
                    input_text=input_text,
                    replacement_text=replacement_text,
                    notes=(raw.get("notes") or "").strip(),
                )
            )
    return entries


def lexicon_row(input_text: str, spoken: str, notes: str | None, source: str) -> dict:
    """Build one lexicon import row from a correction. Mode, case-sensitivity, and
    confidence are derived the same way for every importer (the seed migration, the
    seed re-import, and the legacy user-dict migration)."""

    from app.services import pronounce_convert  # local import avoids an import cycle

    mode = pronounce_convert.classify_mode(input_text, spoken, notes)
    return {
        "mode": mode,
        "spoken": spoken,
        "ipa": None,
        "case_sensitive": pronounce_convert.default_case_sensitive(input_text, mode),
        "confidence": pronounce_convert.CONF_CURATED,
        "source": source,
        "notes": notes,
    }


def build_lexicon_rows(entries: list[SeedEntry]) -> dict[str, dict]:
    """``{input_text: lexicon_row}`` for a seed-entry list (origin ``seed``)."""

    return {
        e.input_text: lexicon_row(e.input_text, e.replacement_text, e.notes, "seed")
        for e in entries
        if e.input_text and e.replacement_text
    }


def format_reference(
    entries: list[SeedEntry], user_dict: dict[str, str] | None = None
) -> str:
    """Format the full correction set as an LLM reference for the pronunciation
    pass: one ``- input -> replacement  (notes)`` line per term.

    Unlike the deterministic dictionary, the reference is NOT category-curated --
    the LLM sees every seed row (homographs, acronyms, brands, slang) plus the
    operator's user dictionary, and decides by context what to apply. The user dictionary is layered on top: it wins on key
    collision (replacing the seed's spelling in place) and appends its own keys.
    Returns "" when there is nothing to reference.
    """

    merged: dict[str, tuple[str, str]] = {}
    for entry in entries:
        if not entry.input_text or not entry.replacement_text:
            continue
        merged.setdefault(entry.input_text, (entry.replacement_text, entry.notes))
    for key, value in (user_dict or {}).items():
        if key and value:
            merged[key] = (value, "")

    lines: list[str] = []
    for inp, (repl, notes) in merged.items():
        line = f"- {inp} -> {repl}"
        if notes:
            line += f"  ({notes})"
        lines.append(line)
    return "\n".join(lines)


def load_reference(user_dict: dict[str, str] | None = None) -> str:
    """Load the bundled seed and format the full correction set (seed + user
    dictionary) as the LLM pronunciation reference."""

    return format_reference(load_seed(seed_path()), user_dict)
