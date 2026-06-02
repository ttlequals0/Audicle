from __future__ import annotations

import csv
from pathlib import Path

import pytest
from app.services import corrections, seed_corrections
from app.services.seed_corrections import SeedEntry

# The bundled list is expected to grow, so tests derive the row count from the
# file rather than hardcoding it; the known categories are asserted as a subset.
_KNOWN_CATEGORIES = {
    "Tech Acronym",
    "Mispronounced Word",
    "Homograph",
    "General Acronym",
    "Consumer Brand",
    "Tech Brand",
    "Format",
    "Medical/Scientific",
    "Symbol/Function",
    "File Path",
}


def _by_input(entries: list[SeedEntry]) -> dict[str, SeedEntry]:
    return {e.input_text: e for e in entries}


def _csv_row_count(path: Path) -> int:
    with path.open(newline="", encoding="utf-8") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def test_load_seed_parses_every_csv_row() -> None:
    path = seed_corrections.seed_path()
    entries = seed_corrections.load_seed(path)
    # No rows dropped by parsing (catches quoting/column bugs as the list grows).
    assert len(entries) == _csv_row_count(path) > 0
    assert all(e.category and e.input_text and e.replacement_text for e in entries)


def test_load_seed_known_categories_present() -> None:
    cats = {e.category for e in seed_corrections.load_seed(seed_corrections.seed_path())}
    assert cats >= _KNOWN_CATEGORIES


def test_annotated_homograph_not_applicable() -> None:
    entries = _by_input(seed_corrections.load_seed(seed_corrections.seed_path()))
    row = entries["read (present)"]
    assert row.category == "Homograph"
    assert row.applicable is False


def test_multiword_brand_is_applicable() -> None:
    entries = _by_input(seed_corrections.load_seed(seed_corrections.seed_path()))
    row = entries["Louis Vuitton"]
    assert row.applicable is True


def test_spelled_out_acronym_not_applicable() -> None:
    entries = _by_input(seed_corrections.load_seed(seed_corrections.seed_path()))
    assert entries["API"].applicable is False  # 'A P I' -- LLM cleanup handles this
    assert entries["CEO"].applicable is False


def test_spelled_out_acronym_excluded_regardless_of_category() -> None:
    """A spelled-out acronym in a non-acronym category (Tech Brand 'AWS' ->
    'A W S') must still be excluded -- the cleanup stage dots it anyway."""

    entries = _by_input(seed_corrections.load_seed(seed_corrections.seed_path()))
    aws = entries["AWS"]
    assert aws.category == "Tech Brand"
    assert aws.applicable is False


def test_pronounce_as_word_acronym_is_applicable() -> None:
    entries = _by_input(seed_corrections.load_seed(seed_corrections.seed_path()))
    assert entries["RAM"].applicable is True  # 'ram' is not a letter spell-out
    assert entries["BIOS"].applicable is True  # 'BY-oss'
    assert entries["SQL"].applicable is True  # 'sequel'


def test_ai_seed_uses_spaced_form_not_dotted() -> None:
    # 'AI' is spelled with spaces, not periods -- XTTS reads a period as a pause
    # ("A <pause> I"). Like other all-caps spelled-out rows it is NOT in the
    # applicable dict (the deterministic acronym speller produces "A I"); it stays
    # in the seed for the LLM reference.
    entries = _by_input(seed_corrections.load_seed(seed_corrections.seed_path()))
    assert entries["AI"].replacement_text == "A I"
    assert "." not in entries["AI"].replacement_text
    assert entries["AI"].applicable is False
    applied = seed_corrections.applicable_dict(seed_corrections.load_seed(seed_corrections.seed_path()))
    assert "AI" not in applied


def test_mixed_case_spelled_out_token_is_applicable() -> None:
    # The LLM dot-spells all-caps tokens, so a mixed-case spelled-out row
    # ('ttyS0' -> 'T T Y S 0') is the only thing that voices it -- keep it applied.
    entries = _by_input(seed_corrections.load_seed(seed_corrections.seed_path()))
    assert entries["ttyS0"].applicable is True
    # All-caps spelled-out tokens stay excluded (cleanup dots them).
    assert entries["GRUB"].applicable is False


def test_applicable_dict_excludes_non_applicable_rows() -> None:
    entries = seed_corrections.load_seed(seed_corrections.seed_path())
    applied = seed_corrections.applicable_dict(entries)
    # Excluded: annotated homographs and spelled-out acronyms.
    assert "read (present)" not in applied
    assert "API" not in applied
    # Included: a brand phrase and a pronounce-as-word acronym.
    assert applied["Louis Vuitton"] == "loo-ee vwee-TOHN"
    assert applied["SQL"] == "sequel"
    # Every applicable entry is present and nothing else.
    expected = {e.input_text for e in entries if e.applicable}
    assert set(applied) == expected


# --- format_reference() / load_reference() ---------------------------------


def test_format_reference_includes_every_category_with_notes() -> None:
    entries = [
        SeedEntry("Homograph", "read (present)", "reed", "Present tense", False),
        SeedEntry("Consumer Brand", "Porsche", "POR-shuh", "", True),
        SeedEntry("Tech Acronym", "API", "A P I", "spell out", False),
        SeedEntry("Symbol/Function", "kmalloc", "kay malloc", "", True),
    ]
    block = seed_corrections.format_reference(entries)
    # Full set: no category curation, no applicability gating -- even the
    # non-applicable annotated homograph and the spelled-out acronym appear.
    assert "read (present) -> reed  (Present tense)" in block
    assert "Porsche -> POR-shuh" in block
    assert "API -> A P I  (spell out)" in block
    assert "kmalloc -> kay malloc" in block


def test_format_reference_layers_user_dict_over_seed() -> None:
    entries = [SeedEntry("Consumer Brand", "Porsche", "POR-shuh", "note", True)]
    block = seed_corrections.format_reference(entries, {"Porsche": "PORSH", "Kubernetes": "koo-ber-net-eez"})
    # User wins on collision (seed note dropped) and adds its own keys.
    assert "Porsche -> PORSH" in block
    assert "POR-shuh" not in block
    assert "Kubernetes -> koo-ber-net-eez" in block


def test_format_reference_skips_blank_rows() -> None:
    entries = [SeedEntry("Homograph", "", "x", "", False), SeedEntry("Homograph", "y", "", "", False)]
    assert seed_corrections.format_reference(entries) == ""


def test_load_reference_from_bundled_csv() -> None:
    block = seed_corrections.load_reference()
    assert block  # the shipped CSV has rows
    # A known homograph carries its context annotation through to the reference.
    assert "read (present) -> reed" in block
    # An acronym the deterministic pass skips is still shown to the LLM.
    assert "API ->" in block


# --- applicable_dict() edge cases ------------------------------------------


def test_applicable_dict_duplicate_key_first_wins() -> None:
    entries = [
        SeedEntry("Tech Brand", "Acme", "ACK-mee", "", True),
        SeedEntry("Tech Brand", "Acme", "ay-see-em-ee", "dup", True),
    ]
    assert seed_corrections.applicable_dict(entries) == {"Acme": "ACK-mee"}


# --- load_seed() error handling --------------------------------------------


def test_load_seed_missing_file_returns_empty(tmp_path: Path) -> None:
    assert seed_corrections.load_seed(tmp_path / "nope.csv") == []


def test_load_seed_malformed_columns_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("word,pronounce\nAPI,A P I\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must have columns"):
        seed_corrections.load_seed(bad)


def test_load_seed_row_failing_validation_is_not_applicable(tmp_path: Path) -> None:
    over_limit = "x" * 201  # exceeds MAX_VALUE_CHARS (200)
    csv_text = (
        "category,input_text,replacement_text,notes\n"
        f"Mispronounced Word,widget,{over_limit},too long\n"
    )
    path = tmp_path / "seed.csv"
    path.write_text(csv_text, encoding="utf-8")
    entries = seed_corrections.load_seed(path)
    assert len(entries) == 1
    assert entries[0].applicable is False
    assert seed_corrections.applicable_dict(entries) == {}
