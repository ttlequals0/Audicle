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
  substitution. The LLM pronunciation pass handles them by context (it sees the
  full reference via :func:`format_reference`); the deterministic pass skips them.
- ALL-CAPS tokens whose replacement is the spelled-out letters (``API`` ->
  ``A P I``, ``AWS`` -> ``A W S``): the LLM cleanup stage already spells these
  out (to dotted form) before corrections run, so the row would never match the
  cleaned text. Pronounce-as-word forms (``RAM`` -> ``ram``) and expansions
  (``TL;DR`` -> ``too long didn't read``) stay applicable.
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
# A bare alphanumeric token with no separators (so "CI/CD", "TL;DR" are excluded
# and stay applicable -- the LLM's all-caps rule only fires on tokens like these).
_SINGLE_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{2,}$")


@dataclass(frozen=True)
class SeedEntry:
    category: str
    input_text: str
    replacement_text: str
    notes: str
    applicable: bool  # True -> merged into the pipeline correction dictionary


def seed_path() -> Path:
    return Path(__file__).parent.parent / "defaults" / "tts_correction_list.csv"


def _is_spelled_out(replacement: str) -> bool:
    """True when the replacement is letter-by-letter spaced spelling (``A P I``)."""

    tokens = replacement.split()
    return len(tokens) > 1 and all(len(tok) == 1 for tok in tokens)


def _is_applicable(input_text: str, replacement_text: str) -> bool:
    """Whether a row is applied to text in the pipeline."""

    # Context-dependent annotated rows (homographs) can't be whole-word matched.
    if _ANNOTATION_RE.search(input_text):
        return False
    # Spelled-out ALL-CAPS tokens duplicate what LLM cleanup already produces,
    # regardless of category (Tech Brand acronyms like AWS land here too). The
    # LLM's rule only fires on all-caps tokens, so a mixed-case spelled-out row
    # (e.g. "ttyS0" -> "T T Y S 0") stays applicable -- nothing else voices it.
    if (
        _SINGLE_TOKEN_RE.match(input_text)
        and _is_spelled_out(replacement_text)
        and input_text == input_text.upper()
    ):
        return False
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
        return False
    return True


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
                    applicable=_is_applicable(input_text, replacement_text),
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


def applicable_dict(entries: list[SeedEntry]) -> dict[str, str]:
    """``{input_text: replacement_text}`` for applicable rows; first row wins on dup."""

    result: dict[str, str] = {}
    for entry in entries:
        if not entry.applicable:
            continue
        if entry.input_text in result:
            logger.warning(
                "Duplicate seed correction key; keeping first",
                extra={"event": "seed_dup_key", "key": entry.input_text},
            )
            continue
        result[entry.input_text] = entry.replacement_text
    return result


def load_applicable_dict() -> dict[str, str]:
    """Load the bundled seed and return its applicable ``{key: replacement}`` map."""

    return applicable_dict(load_seed(seed_path()))


def format_reference(
    entries: list[SeedEntry], user_dict: dict[str, str] | None = None
) -> str:
    """Format the full correction set as an LLM reference for the pronunciation
    pass: one ``- input -> replacement  (notes)`` line per term.

    Unlike the deterministic dictionary, the reference is NOT category-curated
    and NOT gated by ``applicable`` -- the LLM sees every seed row (homographs,
    acronyms, brands, slang) plus the operator's user dictionary, and decides by
    context what to apply. The user dictionary is layered on top: it wins on key
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
