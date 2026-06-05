# ruff: noqa: RUF001  (IPA symbols in test fixtures are intentional)
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


def test_convert_entry_derives_ipa_from_spoken() -> None:
    entry = pc.convert_entry("Kubernetes", spoken="koo-BER-neh-tees")
    assert entry.mode == "override"
    assert entry.spoken == "koo-BER-neh-tees"
    # gruut is installed in the dev env, so IPA is derived.
    assert entry.ipa and pc.validate_ipa(entry.ipa)


def test_convert_entry_derives_spoken_from_ipa() -> None:
    # IPA-only input must still yield a (lossy) spoken form, at lower confidence.
    entry = pc.convert_entry("Worcester", ipa="wˈʊstɚ")
    assert entry.spoken  # derived, non-empty
    assert entry.confidence <= pc.CONF_SPOKEN_FROM_IPA
    assert entry.ipa == "wˈʊstɚ"


def test_validate_ipa_rejects_ascii() -> None:
    assert pc.validate_ipa("wˈʊstɚ") is True
    assert pc.validate_ipa("worcester") is False  # ASCII letters => not phonemized
    assert pc.validate_ipa("") is False


def test_ipa_to_respelling_maps_phonemes() -> None:
    out = pc.ipa_to_respelling("kˈæt")  # "cat"
    assert "a" in out  # æ -> "a"
    assert out  # non-empty
