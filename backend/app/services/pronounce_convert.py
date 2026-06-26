"""Shared pronunciation-entry conversion.

One routine, ``convert_entry``, used by the offline base-lexicon build, the
existing-seed conversion, and the runtime ``PUT /corrections`` path, so every
correction -- seed, base, or user-supplied -- ends up with a ``mode``, a required
``spoken`` respelling, a ``case_sensitive`` flag, and a ``confidence`` score.

Chatterbox is text-only, so corrections are plain-text respellings applied to the
article before synthesis -- there is no IPA/phoneme path.
"""

from __future__ import annotations

from dataclasses import dataclass

MODES = ("spell", "word", "override")

# Confidence tiers. Curated respellings are trusted; the aggressive base-lexicon
# apply only respells at/above MIN_CONFIDENCE (the offline build marks lossy,
# IPA-derived base rows below it so the gate skips the weak ones).
CONF_CURATED = 1.0
MIN_CONFIDENCE = 0.8


@dataclass(frozen=True)
class ConvertedEntry:
    mode: str
    spoken: str
    case_sensitive: bool
    confidence: float


def classify_mode(input_text: str, spoken: str | None, notes: str | None) -> str:
    """Pick spell/word/override from the spoken form and any notes."""

    toks = (spoken or "").split()
    if len(toks) > 1 and all(len(t) == 1 for t in toks):
        return "spell"
    note = (notes or "").lower()
    if "spell out" in note or "spell-out" in note:
        return "spell"
    if "as word" in note or "as a word" in note or "as name" in note or "as a name" in note:
        return "word"
    return "override"


def default_case_sensitive(input_text: str, mode: str) -> bool:
    """Acronyms (spell/word) and all-caps tokens match exact case; the rest fold.

    Keeps "US" (country) distinct from "us" while letting "Kubernetes" match any
    casing in the article.
    """

    if mode in ("spell", "word"):
        return True
    return input_text.isupper()


def convert_entry(
    input_text: str,
    spoken: str | None = None,
    notes: str | None = None,
    case_sensitive: bool | None = None,
) -> ConvertedEntry:
    """Normalize one entry to {mode, spoken, case_sensitive, confidence}.

    ``spoken`` is always produced (required by the schema): supplied verbatim,
    else it falls back to ``input_text``. The offline base-lexicon build derives a
    respelling from its IPA sources before calling this, so ``spoken`` is set by
    the time entries reach here.
    """

    derived_spoken = spoken or input_text
    mode = classify_mode(input_text, derived_spoken, notes)
    cs = case_sensitive if case_sensitive is not None else default_case_sensitive(input_text, mode)
    return ConvertedEntry(
        mode=mode, spoken=derived_spoken, case_sensitive=cs, confidence=CONF_CURATED
    )
