from __future__ import annotations

import collections
from pathlib import Path

import pytest
from app.services import seed_corrections
from app.services.seed_corrections import SeedEntry

# --- load_seed() against the bundled CSV -----------------------------------

_EXPECTED_COUNTS = {
    "Tech Acronym": 45,
    "Mispronounced Word": 37,
    "Homograph": 34,
    "General Acronym": 29,
    "Consumer Brand": 27,
    "Tech Brand": 26,
    "Format": 26,
    "Medical/Scientific": 14,
}


def _by_input(entries: list[SeedEntry]) -> dict[str, SeedEntry]:
    return {e.input_text: e for e in entries}


def test_load_seed_parses_all_rows_and_category_counts() -> None:
    entries = seed_corrections.load_seed(seed_corrections.seed_path())
    assert len(entries) == sum(_EXPECTED_COUNTS.values()) == 238
    counts = collections.Counter(e.category for e in entries)
    assert dict(counts) == _EXPECTED_COUNTS


def test_annotated_homograph_not_applicable() -> None:
    entries = _by_input(seed_corrections.load_seed(seed_corrections.seed_path()))
    row = entries["read (present)"]
    assert row.category == "Homograph"
    assert row.applicable is False
    assert row.match_key is None


def test_multiword_brand_is_applicable() -> None:
    entries = _by_input(seed_corrections.load_seed(seed_corrections.seed_path()))
    row = entries["Louis Vuitton"]
    assert row.applicable is True
    assert row.match_key == "Louis Vuitton"


def test_spelled_out_acronym_not_applicable() -> None:
    entries = _by_input(seed_corrections.load_seed(seed_corrections.seed_path()))
    assert entries["API"].applicable is False  # 'A P I' -- LLM cleanup handles this
    assert entries["CEO"].applicable is False


def test_pronounce_as_word_acronym_is_applicable() -> None:
    entries = _by_input(seed_corrections.load_seed(seed_corrections.seed_path()))
    assert entries["RAM"].applicable is True  # 'ram' is not a letter spell-out
    assert entries["BIOS"].applicable is True  # 'BY-oss'
    assert entries["SQL"].applicable is True  # 'sequel'


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
    expected = {e.match_key for e in entries if e.applicable}
    assert set(applied) == expected


# --- applicable_dict() edge cases ------------------------------------------


def test_applicable_dict_duplicate_key_first_wins() -> None:
    entries = [
        SeedEntry("Tech Brand", "Acme", "ACK-mee", "", True, "Acme"),
        SeedEntry("Tech Brand", "Acme", "ay-see-em-ee", "dup", True, "Acme"),
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
    assert entries[0].match_key is None
    assert seed_corrections.applicable_dict(entries) == {}
