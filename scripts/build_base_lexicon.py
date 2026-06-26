#!/usr/bin/env python3
# ruff: noqa: RUF001  (ARPAbet/IPA symbols are intentional)
"""Build the base-lexicon artifact from all external sources (offline).

Clones the source repos into a cache dir, derives a plain-text respelling from each
source's IPA, runs every entry through the shared ``pronounce_convert.convert_entry``
to classify mode/case, dedupes across sources by precedence, and writes a JSONL
artifact the backend imports into the read-only ``base`` rows
(``lexicon.sync_base_artifact``).

This is NOT in the request path. It is heavy (hundreds of thousands of entries plus
source clones), so it runs in CI / the operator's environment, not at runtime. IPA is
used here only to derive a respelling -- Chatterbox is text-only, so the artifact stores
plain ``spoken`` text, never IPA.

Usage:
    uv run python scripts/build_base_lexicon.py --out backend/app/data/base_lexicon.jsonl
    uv run python scripts/build_base_lexicon.py --only cmudict,journalism --limit 200   # smoke

Sources and licenses (see the plan's source inventory):
    cmudict        ISC      ARPAbet  -> respelling, mode=word
    wiktionary     MIT      IPA      -> respelling, mode=word
    usa_cities     CC0      names    -> respelling, mode=override
    world_cities   ODbL     names    (attribution required; opt in with --odbl)
    balacoon       (safe)   abbrev   -> spell/word
    journalism     curated  acronyms -> mode=word
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

# Make the backend package importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.services import pronounce_convert as pc

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("build_base_lexicon")

# Source precedence (lower wins on duplicate surface form): curated/abbreviation
# layers first, then dictionaries, then place names.
_PRECEDENCE = [
    "journalism", "balacoon", "wikiabbrev", "wiktionary", "islex",
    "cmudict", "usa_cities", "world_cities",
]

_REPOS = {
    "cmudict": "https://github.com/words/cmu-pronouncing-dictionary",
    "wiktionary": "https://github.com/DanielSWolf/wiki-pronunciation-dict",
    "usa_cities": "https://github.com/grammakov/USA-cities-and-states",
    "world_cities": "https://github.com/dr5hn/countries-states-cities-database",
    # ODbL (world_cities): attribution recorded in THIRD_PARTY_NOTICES.md (the
    # built artifact's derived city names are ODbL share-alike).
    "wikiabbrev": "https://github.com/google-research-datasets/WikipediaAbbreviationData",
}

# Contemporary news/media acronyms read as words (the "journalism" layer).
_JOURNALISM = [
    "NATO", "OPEC", "WHO", "IMF", "ECB", "UNESCO", "UNICEF", "NAFTA", "ASEAN",
    "BRICS", "OECD", "NASA", "FEMA", "FIFA", "UEFA",
]

# ARPAbet (CMUdict, stress digits stripped) -> IPA.
_ARPABET_IPA = {
    "AA": "ɑ", "AE": "æ", "AH": "ʌ", "AO": "ɔ", "AW": "aʊ", "AY": "aɪ", "B": "b",
    "CH": "tʃ", "D": "d", "DH": "ð", "EH": "ɛ", "ER": "ɚ", "EY": "eɪ", "F": "f",
    "G": "ɡ", "HH": "h", "IH": "ɪ", "IY": "i", "JH": "dʒ", "K": "k", "L": "l",
    "M": "m", "N": "n", "NG": "ŋ", "OW": "oʊ", "OY": "ɔɪ", "P": "p", "R": "ɹ",
    "S": "s", "SH": "ʃ", "T": "t", "TH": "θ", "UH": "ʊ", "UW": "u", "V": "v",
    "W": "w", "Y": "j", "Z": "z", "ZH": "ʒ",
}


def _clone(name: str, cache: Path) -> Path | None:
    dest = cache / name
    if dest.exists():
        return dest
    url = _REPOS.get(name)
    if not url:
        return None
    cache.mkdir(parents=True, exist_ok=True)
    logger.info("Cloning %s ...", url)
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dest)],
            check=True,
            capture_output=True,
            timeout=600,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("Clone failed for %s: %s", name, exc)
        return None
    return dest


def _arpabet_to_ipa(arpa: str) -> str:
    out = []
    for tok in arpa.split():
        sym = "".join(ch for ch in tok if not ch.isdigit())
        if sym in _ARPABET_IPA:
            out.append(_ARPABET_IPA[sym])
    return "".join(out)


# --- IPA -> respelling (offline build only) ---------------------------------
# The runtime is text-only (no IPA), but the dictionary sources (CMUdict/Wiktionary/
# ISLEX) carry only IPA, so the build derives a plain-text respelling from it here and
# feeds that to pronounce_convert as ordinary ``spoken`` text. The derivation is lossy,
# so these entries are marked below MIN_CONFIDENCE and the runtime's aggressive base
# apply skips the weak ones.
_CONF_SPOKEN_FROM_IPA = 0.55

# Minimal IPA -> pseudo-phonetic respelling map (digraphs first so they win). Lossy on
# purpose: a readable approximation for the engine. Stress/length marks are dropped.
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
_IPA_KEYS = sorted(_IPA_RESPELL, key=len, reverse=True)
_STRESS_CHARS = "ˈˌ.ːˑ"
_IPA_HINT_RE = re.compile(r"[^\x00-\x7f]|[ˈˌːˑ]")


def _validate_ipa(ipa: str) -> bool:
    """True if ``ipa`` looks phonemized (a non-ASCII symbol or a stress/length mark)."""

    if not ipa or not ipa.strip():
        return False
    return bool(_IPA_HINT_RE.search(ipa))


def _ipa_to_respelling(ipa: str) -> str:
    """Lossy IPA -> pseudo-phonetic respelling. Stress marks dropped."""

    out: list[str] = []
    for token in ipa.split():
        cleaned = "".join(ch for ch in token if ch not in _STRESS_CHARS)
        syll: list[str] = []
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


def _find_json(root: Path, *names: str) -> Path | None:
    for name in names:
        hits = list(root.rglob(name))
        if hits:
            return hits[0]
    return None


# CMUdict ships as a JS module (`export const dictionary = { "word": "AA1 R", ... }`),
# so parse the "key": "value" lines directly rather than as JSON.
_CMU_LINE_RE = re.compile(r'^\s*"((?:[^"\\]|\\.)+)":\s*"([^"]*)"')


def source_cmudict(cache: Path, limit: int | None) -> Iterator[tuple[str, dict]]:
    root = _clone("cmudict", cache)
    if not root:
        return
    js = root / "index.js"
    if not js.exists():
        logger.warning("cmudict index.js not found")
        return
    n = 0
    for line in js.read_text(encoding="utf-8").splitlines():
        if limit and n >= limit:
            break
        m = _CMU_LINE_RE.match(line)
        if not m:
            continue
        word, arpa = m.group(1), m.group(2)
        if not word or "(" in word or word.startswith("'"):  # skip variants/clitics
            continue
        ipa = _arpabet_to_ipa(arpa)
        if not ipa:
            continue
        yield word, {"ipa": ipa, "notes": "as word"}
        n += 1


def source_usa_cities(cache: Path, limit: int | None) -> Iterator[tuple[str, dict]]:
    root = _clone("usa_cities", cache)
    if not root:
        return
    csv_path = _find_json(root, "us_cities_states_counties.csv", "*.csv")
    if not csv_path:
        return
    seen: set[str] = set()
    with csv_path.open(encoding="utf-8", errors="ignore") as fh:
        reader = csv.reader(fh, delimiter="|")
        next(reader, None)  # header
        for row in reader:
            if limit and len(seen) >= limit:
                break
            if not row:
                continue
            city = row[0].strip()
            if not city or city in seen or not city[0].isupper():
                continue
            seen.add(city)
            yield city, {}  # name only; spoken falls back to the surface form


def source_journalism(_cache: Path, limit: int | None) -> Iterator[tuple[str, dict]]:
    for i, acro in enumerate(_JOURNALISM):
        if limit and i >= limit:
            break
        yield acro, {"spoken": acro, "notes": "as word"}


# IPA pulled from Wiktionary slashes/brackets; keep the inner phoneme string only.
_IPA_WRAP_RE = re.compile(r"[/\[\]]")


def source_wiktionary(cache: Path, limit: int | None) -> Iterator[tuple[str, dict]]:
    """Wiktionary-derived word->IPA. The repo ships dictionaries/en.json as
    ``{word: ["i p a", ...]}`` (space-separated phonemes); take the first IPA."""

    root = _clone("wiktionary", cache)
    if not root:
        return
    path = root / "dictionaries" / "en.json"  # NOT en-metadata.json
    if not path.exists():
        logger.warning("wiktionary dictionaries/en.json not found")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return
    for n, (word, ipas) in enumerate(data.items() if isinstance(data, dict) else []):
        if limit and n >= limit:
            break
        ipa = ipas[0] if isinstance(ipas, list) and ipas else ipas
        if not isinstance(word, str) or not isinstance(ipa, str) or not word:
            continue
        yield word, {"ipa": _IPA_WRAP_RE.sub("", ipa).strip(), "notes": "as word"}


def source_islex(_cache: Path, limit: int | None) -> Iterator[tuple[str, dict]]:
    """ISLEX via pysle (bundled lexicon, IPA). Lazy import; skip if absent."""

    try:
        from pysle import isletool
    except Exception:
        logger.warning("pysle not installed; skipping islex")
        return
    try:
        isle = isletool.Isle()
        words = list(isle.rawData.keys()) if hasattr(isle, "rawData") else []
    except Exception:
        logger.warning("pysle ISLEX load failed; skipping", exc_info=True)
        return
    for i, word in enumerate(words):
        if limit and i >= limit:
            break
        try:
            entries = isle.lookup(word)
            phones = "".join(entries[0].phonemeList.phonemes) if entries else ""
        except Exception:
            continue
        if word and phones:
            yield word, {"ipa": phones, "notes": "as word"}


def source_balacoon(_cache: Path, limit: int | None) -> Iterator[tuple[str, dict]]:
    """balacoon/en_us_abbreviations (HF). Plain token lists, no extension:
    `{subset}/abbreviations` (spell out) and `{subset}/words` (read as a word).
    wiki is the clean set, kestrel the larger noisy one; wiki wins on dup."""

    try:
        from huggingface_hub import hf_hub_download
    except Exception:
        logger.warning("huggingface_hub not installed; skipping balacoon")
        return
    seen: set[str] = set()
    n = 0
    for subset in ("wiki", "kestrel"):
        for kind, notes, spell in (("abbreviations", "spell out", True), ("words", "as word", False)):
            try:
                path = hf_hub_download(
                    "balacoon/en_us_abbreviations", f"{subset}/{kind}", repo_type="dataset"
                )
            except Exception:
                logger.warning("balacoon download failed for %s/%s", subset, kind)
                continue
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if limit and n >= limit:
                        return
                    tok = line.strip()
                    if not tok or tok in seen:
                        continue
                    seen.add(tok)
                    n += 1
                    yield tok, {"spoken": " ".join(tok) if spell else tok, "notes": notes}


# textproto token: `expanded: "research" abbreviated: "resrch"` (expanded first).
_WIKIABBR_RE = re.compile(
    r'expanded:\s*"((?:[^"\\]|\\.)*)"\s*abbreviated:\s*"((?:[^"\\]|\\.)*)"'
)


def source_wikiabbrev(cache: Path, limit: int | None) -> Iterator[tuple[str, dict]]:
    """google WikipediaAbbreviationData: data/*.textproto with token blocks that
    pair an `abbreviated` form with its `expanded` reading. Regex the pairs out
    rather than compiling the .proto. Keys shorter than 3 chars are skipped --
    "r"->"are"/"u"->"you" would clobber real single letters."""

    root = _clone("wikiabbrev", cache)
    if not root:
        return
    seen: set[str] = set()
    for path in root.rglob("*.textproto"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for expanded, abbreviated in _WIKIABBR_RE.findall(text):
            if limit and len(seen) >= limit:
                return
            abbr = abbreviated.replace("\\'", "'").strip()
            exp = expanded.replace("\\'", "'").strip()
            if len(abbr) < 3 or not exp or abbr in seen:
                continue
            seen.add(abbr)
            # Low confidence: this is noisy crowd data whose "abbreviated" forms
            # include real words ("the" abbreviating "these"). Below the base-lexicon
            # apply gate so it is ingested/searchable but never deterministically applied.
            yield abbr, {"spoken": exp, "notes": f"abbreviation of {exp}", "confidence": 0.4}


def source_world_cities(cache: Path, limit: int | None) -> Iterator[tuple[str, dict]]:
    """countries-states-cities (ODbL): global place names from cities.json."""

    root = _clone("world_cities", cache)
    if not root:
        return
    # The repo ships cities nested inside the combined dump (countries -> states ->
    # cities), not a flat cities.json. Walk it and collect every "name".
    path = _find_json(root, "cities.json", "countries+states+cities.json", "countries+cities.json")
    if not path:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return
    seen: set[str] = set()

    def _walk(node):
        if isinstance(node, dict):
            name = node.get("name")
            # A city node has a name but no nested "states"/"cities" children.
            if isinstance(name, str) and "states" not in node and "cities" not in node:
                yield name
            for key in ("states", "cities"):
                yield from _walk(node.get(key, []))
        elif isinstance(node, list):
            for item in node:
                yield from _walk(item)

    for name in _walk(data):
        if limit and len(seen) >= limit:
            break
        name = name.strip()
        if not name or name in seen or not name[0].isalpha():
            continue
        seen.add(name)
        yield name, {}  # name only; spoken falls back to the surface form


_SOURCES = {
    "cmudict": source_cmudict,
    "usa_cities": source_usa_cities,
    "world_cities": source_world_cities,
    "journalism": source_journalism,
    "wiktionary": source_wiktionary,
    "islex": source_islex,
    "balacoon": source_balacoon,
    "wikiabbrev": source_wikiabbrev,
}


def build(out_path: Path, only: list[str], limit: int | None, cache: Path) -> dict[str, int]:
    """Run the sources, dedupe by precedence, write JSONL. Returns per-source counts."""

    chosen = [s for s in _PRECEDENCE if s in _SOURCES and (not only or s in only)]
    merged: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for name in chosen:
        n = 0
        for surface, hint in _SOURCES[name](cache, limit):
            if surface in merged:  # earlier (higher-precedence) source wins
                continue
            # IPA-native sources (CMUdict/Wiktionary/ISLEX) carry only IPA, so derive a
            # plain-text respelling here and mark it low-confidence (lossy); the rest
            # already supply ``spoken`` (or fall back to the surface form).
            spoken = hint.get("spoken")
            ipa = hint.get("ipa")
            confidence = hint.get("confidence")
            if not spoken and ipa and _validate_ipa(ipa):
                spoken = _ipa_to_respelling(ipa) or None
                if confidence is None:
                    confidence = _CONF_SPOKEN_FROM_IPA
            converted = pc.convert_entry(surface, spoken=spoken, notes=hint.get("notes"))
            merged[surface] = {
                "origin": "base",
                "input_text": surface,
                "mode": converted.mode,
                "spoken": converted.spoken,
                "case_sensitive": converted.case_sensitive,
                "confidence": confidence if confidence is not None else converted.confidence,
                "source": name,
                "notes": hint.get("notes"),
            }
            n += 1
        counts[name] = n
        logger.info("source %s -> %d entries", name, n)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for entry in merged.values():
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    logger.info("Wrote %d entries to %s", len(merged), out_path)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the base-lexicon artifact.")
    parser.add_argument("--out", default="backend/app/data/base_lexicon.jsonl")
    parser.add_argument("--only", default="", help="comma-separated source subset")
    parser.add_argument("--limit", type=int, default=None, help="cap entries per source (smoke)")
    parser.add_argument("--cache", default=".lexicon_cache", help="repo clone cache dir")
    args = parser.parse_args()
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    counts = build(Path(args.out), only, args.limit, Path(args.cache))
    logger.info("Done: %s", counts)


if __name__ == "__main__":
    main()
