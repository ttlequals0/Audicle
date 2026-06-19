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
    "Tech Brand",
    "Format",
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


def test_load_seed_keeps_real_word_swaps() -> None:
    # Acronym auto-spelling was removed; real-word swaps and multi-word expansions stay.
    entries = _by_input(seed_corrections.load_seed(seed_corrections.seed_path()))
    assert entries["SQL"].replacement_text == "sequel"
    assert entries["SOC 2"].replacement_text == "sock two"


# --- format_reference() / load_reference() ---------------------------------


def test_format_reference_includes_every_category_with_notes() -> None:
    entries = [
        SeedEntry("Homograph", "read (present)", "reed", "Present tense"),
        SeedEntry("Consumer Brand", "Porsche", "POR-shuh", ""),
        SeedEntry("Symbol/Function", "kmalloc", "kay malloc", ""),
    ]
    block = seed_corrections.format_reference(entries)
    # The reference is not category-curated: every row appears, annotation and all.
    assert "read (present) -> reed  (Present tense)" in block
    assert "Porsche -> POR-shuh" in block
    assert "kmalloc -> kay malloc" in block


def test_format_reference_layers_user_dict_over_seed() -> None:
    entries = [SeedEntry("Consumer Brand", "Porsche", "POR-shuh", "note")]
    block = seed_corrections.format_reference(
        entries, {"Porsche": "PORSH", "Kubernetes": "koo-ber-net-eez"}
    )
    # User wins on collision (seed note dropped) and adds its own keys.
    assert "Porsche -> PORSH" in block
    assert "POR-shuh" not in block
    assert "Kubernetes -> koo-ber-net-eez" in block


def test_format_reference_skips_blank_rows() -> None:
    entries = [SeedEntry("Homograph", "", "x", ""), SeedEntry("Homograph", "y", "", "")]
    assert seed_corrections.format_reference(entries) == ""


def test_load_reference_from_bundled_csv() -> None:
    block = seed_corrections.load_reference()
    assert block  # the shipped CSV has rows
    # A known homograph carries its context annotation through to the reference.
    assert "read (present) -> reed" in block
    # A real-word swap is shown to the LLM.
    assert "SQL -> sequel" in block


# --- load_seed() error handling --------------------------------------------


def test_load_seed_missing_file_returns_empty(tmp_path: Path) -> None:
    assert seed_corrections.load_seed(tmp_path / "nope.csv") == []


def test_load_seed_malformed_columns_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("word,pronounce\nFoo,bar\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must have columns"):
        seed_corrections.load_seed(bad)


# --- merged manual corrections survive the trim ----------------------------


def test_seed_includes_merged_manual_corrections() -> None:
    entries = _by_input(seed_corrections.load_seed(seed_corrections.seed_path()))
    assert entries["OS"].replacement_text == "oh ess"
    assert entries["VMs"].replacement_text == "vee emz"
    assert entries["Opex"].replacement_text == "op eks"
    assert entries["OpenAI"].replacement_text == "open ay eye"  # fixes the prod "opemai" typo
    assert entries["retry"].replacement_text == "ree try"


def _has_spaced_letter_run(text: str) -> bool:
    """True if the text has two or more consecutive single-letter tokens (e.g. 'B S D').
    A lone single letter (the pronoun 'I' in 'as far as I know') is not a run."""

    run = 0
    for token in text.split(" "):
        if len(token) == 1 and token.isalpha():
            run += 1
            if run >= 2:
                return True
        else:
            run = 0
    return False


def test_no_spaced_single_letter_runs() -> None:
    # 0.40.0: every spelled-out abbreviation uses run-together phonetic letter-words
    # ("bee ess dee"), never spaced single letters ("B S D"), for Chatterbox.
    offenders = [
        e.input_text
        for e in seed_corrections.load_seed(seed_corrections.seed_path())
        if _has_spaced_letter_run(e.replacement_text)
    ]
    assert offenders == [], f"de-space these abbreviations: {offenders}"


def test_ebitda_reads_as_a_word() -> None:
    entries = _by_input(seed_corrections.load_seed(seed_corrections.seed_path()))
    assert entries["EBITDA"].replacement_text == "ee bit dah"
