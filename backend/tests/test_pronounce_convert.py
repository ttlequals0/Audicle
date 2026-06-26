from __future__ import annotations

from app.services import pronounce_convert as pc


def test_classify_mode() -> None:
    assert pc.classify_mode("API", "A P I", None) == "spell"
    assert pc.classify_mode("SQL", "sequel", "Pronounce as word") == "word"
    assert pc.classify_mode("Kubernetes", "koo-BER-neh-tees", None) == "override"


def test_default_case_sensitive() -> None:
    # Acronyms / all-caps match exact case; ordinary words fold.
    assert pc.default_case_sensitive("US", "spell") is True
    assert pc.default_case_sensitive("US", "override") is True
    assert pc.default_case_sensitive("Kubernetes", "override") is False


def test_convert_entry_keeps_spoken_and_classifies() -> None:
    entry = pc.convert_entry("Kubernetes", spoken="koo-BER-neh-tees")
    assert entry.mode == "override"
    assert entry.spoken == "koo-BER-neh-tees"
    assert entry.case_sensitive is False
    assert entry.confidence == pc.CONF_CURATED


def test_convert_entry_falls_back_to_input_text() -> None:
    entry = pc.convert_entry("NASA")
    assert entry.spoken == "NASA"
    assert entry.case_sensitive is True  # all-caps stays exact-case
