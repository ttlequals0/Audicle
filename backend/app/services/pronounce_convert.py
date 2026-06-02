# ruff: noqa: RUF001, RUF002, RUF003  (this module deliberately contains IPA
# symbols that ruff flags as "ambiguous" unicode -- the whole point of the map)
"""Shared pronunciation-entry conversion (the kaldi-helpers-style workflow).

One routine, ``convert_entry``, used by the offline base-lexicon build, the
existing-seed conversion, and the runtime ``PUT /corrections`` path, so every
correction -- seed, base, or user-supplied -- ends up with a ``mode``, a required
``spoken`` respelling, an optional ``ipa``, a ``case_sensitive`` flag, and a
``confidence`` score.

Two derivation directions:

- ``spoken`` -> ``ipa``: phonemize a coined respelling ("koo-BER-neh-tees") with
  gruut (the respelling IS the pronunciation guide).
- ``ipa`` -> ``spoken``: the dictionary sources (CMUdict/ISLEX/Wiktionary) carry
  only IPA, so a respelling is derived from it. This direction is lossy; entries
  produced this way get a lower confidence so the aggressive XTTS apply gate can
  skip the weak ones.

gruut is MIT and pure-Python (no torch), imported lazily so the backend stays
importable and fast in environments where the language data is absent.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("app.services.pronounce_convert")

MODES = ("spell", "word", "override")

# Confidence tiers. Curated respellings are trusted; an IPA-derived respelling is
# lossy. The aggressive XTTS apply (A7) only respells at/above MIN_XTTS_CONFIDENCE.
CONF_CURATED = 1.0
CONF_IPA_FROM_SPOKEN = 0.85
CONF_SPOKEN_FROM_IPA = 0.55
MIN_XTTS_CONFIDENCE = 0.8

_SINGLE_LETTERS_RE = re.compile(r"^(?:[A-Za-z] )+[A-Za-z]$")

# Minimal IPA (gruut/gruut-ipa symbols) -> pseudo-phonetic respelling map. Lossy
# on purpose: it produces a readable approximation for XTTS, and entries built
# this way are marked low-confidence. Stress marks are dropped.
_IPA_RESPELL = {
    "tʃ": "ch", "dʒ": "j", "ʃ": "sh", "ʒ": "zh", "θ": "th", "ð": "th",
    "ŋ": "ng", "j": "y", "ɹ": "r", "r": "r", "ɫ": "l", "l": "l",
    "aɪ": "eye", "aʊ": "ow", "ɔɪ": "oy", "eɪ": "ay", "oʊ": "oh", "oː": "oh",
    "iː": "ee", "i": "ee", "ɪ": "ih", "ɛ": "eh", "æ": "a", "ʌ": "uh",
    "ə": "uh", "ɚ": "er", "ɝ": "er", "ɑ": "ah", "ɑː": "ah", "ɒ": "o",
    "ɔ": "aw", "ɔː": "aw", "ʊ": "oo", "uː": "oo", "u": "oo",
    "b": "b", "d": "d", "f": "f", "ɡ": "g", "g": "g", "h": "h", "k": "k",
    "m": "m", "n": "n", "p": "p", "s": "s", "t": "t", "v": "v", "w": "w",
    "z": "z", "ks": "ks",
}
# Longest IPA symbols first so digraphs ("tʃ", "aɪ") win over single chars.
_IPA_KEYS = sorted(_IPA_RESPELL, key=len, reverse=True)
_STRESS_CHARS = "ˈˌ.ːˑ"


@dataclass(frozen=True)
class ConvertedEntry:
    mode: str
    spoken: str
    ipa: str | None
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


def to_ipa(text: str, lang: str = "en-us") -> str | None:
    """Phonemize ``text`` to an IPA string via gruut, or None if unavailable.

    gruut is imported lazily; any failure (missing language data, odd input)
    degrades to None rather than raising, so the caller can fall back.
    """

    try:
        from gruut import sentences  # lazy import of the optional dependency
    except Exception:
        return None
    try:
        phones: list[str] = []
        for sent in sentences(text, lang=lang):
            for word in sent:
                if word.phonemes:
                    phones.append("".join(word.phonemes))
        joined = " ".join(phones).strip()
        return joined or None
    except Exception:
        logger.debug("gruut phonemization failed", extra={"text": text}, exc_info=True)
        return None


def ipa_to_respelling(ipa: str) -> str:
    """Lossy IPA -> pseudo-phonetic respelling. Stress marks dropped."""

    out: list[str] = []
    for token in ipa.split():
        cleaned = "".join(ch for ch in token if ch not in _STRESS_CHARS)
        syll = []
        i = 0
        while i < len(cleaned):
            for key in _IPA_KEYS:
                if cleaned.startswith(key, i):
                    syll.append(_IPA_RESPELL[key])
                    i += len(key)
                    break
            else:
                i += 1  # skip an unmapped symbol rather than emit noise
        if syll:
            out.append("".join(syll))
    return "-".join(out)


def validate_ipa(ipa: str) -> bool:
    """True if ``ipa`` looks like a phonemized string rather than plain text.

    IPA legitimately uses ASCII consonants (w, s, t, p, b ...), so we can't reject
    on ASCII alone. Instead require at least one IPA-specific marker: a non-ASCII
    symbol (vowels like ɪ, ʊ, ɚ) or a stress/length mark.
    """

    if not ipa or not ipa.strip():
        return False
    return bool(_IPA_HINT_RE.search(ipa))


_IPA_HINT_RE = re.compile(r"[^\x00-\x7f]|[ˈˌːˑ]")


def convert_entry(
    input_text: str,
    spoken: str | None = None,
    ipa: str | None = None,
    notes: str | None = None,
    case_sensitive: bool | None = None,
    derive_ipa: bool = True,
) -> ConvertedEntry:
    """Normalize one entry to {mode, spoken, ipa, case_sensitive, confidence}.

    ``spoken`` is always produced (required by the schema): supplied verbatim,
    else derived from ``ipa``, else falls back to ``input_text``. ``ipa`` is kept
    if valid, else derived from the spoken form via gruut -- unless ``derive_ipa``
    is False (bulk builds skip the per-entry gruut call for speed; the phoneme
    engine phonemizes those terms at synth time instead).
    """

    confidence = CONF_CURATED
    derived_spoken = spoken

    if not derived_spoken:
        if ipa and validate_ipa(ipa):
            derived_spoken = ipa_to_respelling(ipa) or input_text
            confidence = CONF_SPOKEN_FROM_IPA
        else:
            derived_spoken = input_text

    mode = classify_mode(input_text, derived_spoken, notes)

    out_ipa: str | None
    if ipa and validate_ipa(ipa):
        out_ipa = ipa
    elif derive_ipa:
        # Derive IPA from the surface form so the entry is usable on the phoneme
        # engine. A coined respelling phonemizes more faithfully than the input.
        out_ipa = to_ipa(derived_spoken) or to_ipa(input_text)
        if out_ipa and spoken:
            confidence = min(confidence, CONF_IPA_FROM_SPOKEN)
    else:
        out_ipa = None

    cs = case_sensitive if case_sensitive is not None else default_case_sensitive(input_text, mode)
    return ConvertedEntry(
        mode=mode, spoken=derived_spoken, ipa=out_ipa, case_sensitive=cs, confidence=confidence
    )
