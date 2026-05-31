from __future__ import annotations

import csv
from pathlib import Path

import pytest
from app.services import seed_corrections
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


# --- reference_block() / load_reference_block() ----------------------------


def test_reference_block_includes_only_curated_categories() -> None:
    entries = [
        SeedEntry("Homograph", "read (present)", "reed", "Present tense", False),
        SeedEntry("Consumer Brand", "Porsche", "POR-shuh", "", True),
        SeedEntry("Tech Acronym", "API", "A P I", "spell out", False),
        SeedEntry("Symbol/Function", "kmalloc", "kay malloc", "", True),
    ]
    block = seed_corrections.reference_block(entries, seed_corrections.REFERENCE_CATEGORIES)
    assert "read (present) -> reed  (Present tense)" in block
    assert "Porsche -> POR-shuh" in block
    # Categories outside the curated set are left to the deterministic pass.
    assert "API" not in block
    assert "kmalloc" not in block


def test_reference_block_empty_when_no_match() -> None:
    entries = [SeedEntry("Tech Acronym", "API", "A P I", "", False)]
    assert seed_corrections.reference_block(entries, seed_corrections.REFERENCE_CATEGORIES) == ""


def test_load_reference_block_from_bundled_csv() -> None:
    block = seed_corrections.load_reference_block()
    assert block  # the shipped CSV has curated rows
    # A known homograph carries its context annotation through to the reference.
    assert "read (present) -> reed" in block


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
