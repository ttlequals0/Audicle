# Remove pseudo-phonetic respellings

Date: 2026-06-16
Status: approved

## Problem

The seed pronunciation list respells hundreds of words and names as hyphenated
pseudo-phonetic syllables (`Kubernetes -> koo-ber-neh-tees`, `pseudonym ->
soo-doh-nim`, `particularly -> par-tik-yoo-ler-lee`, `Nike -> ny-kee`). Chatterbox
reads the hyphenated syllables choppily -- they sound worse than its native
pronunciation of the plain word. The 0.33.0 lowercase-stress fold helped the casing
but not the underlying problem: the respelling *mechanism* itself does not work for
this engine. The operator wants this whole class removed.

## Decision

Hard-delete every hyphenated pseudo-phonetic respelling from the seed, plus the month
respellings in code. Rely on Chatterbox's native pronunciation, with acronym
letter-spelling as the only remaining fallback. Permanent (not a runtime toggle).

Scope confirmed with the operator:
- All 396 hyphenated seed respellings (including acronym-read-as-word forms like
  `YAML -> yam-el`).
- The 12 month respellings.
- Base lexicon is OUT of scope: its 7,712 "hyphenated" applied rows are all identity
  mappings (`spoken == input`) for city names that natively contain a hyphen
  (`Mazar-e Sharif`, `Blairsden-Graeagle`). It has zero phonetic respellings, so there
  is nothing to remove there and removing them would corrupt real names.

## What is removed

1. **Seed CSV** (`backend/app/defaults/tts_correction_list.csv`): the 396 rows whose
   `replacement_text` is a phonetic respelling, identified by a lowercase-hyphen-
   lowercase pattern in the replacement AND `replacement != input_text`. Before
   applying, dump the to-be-removed set and eyeball it for false positives (a legit
   hyphenated real name mapped to itself should NOT be removed; the
   `replacement != input` guard already excludes identity rows).

2. **Month respellings** (`backend/app/services/pipeline.py`): delete `_MONTH_RESPELL`,
   `_MONTH_RE`, `_normalize_months`, and the `_normalize_months(...)` call inside
   `_normalize_for_tts`. Keep `_normalize_date_months` (abbreviation expansion
   `Jan -> January` is not a phonetic respelling and stays).

3. **The 0.33.0 A3 band-aid** (`pipeline.py` + `pronunciation.txt`): remove the
   `respell_canon` case-fold block in `_apply_corrections` and the lowercase-stress
   instruction added to `backend/app/defaults/pronunciation.txt`. With no respellings
   left to fold, both are dead.

## What is kept (must not regress)

- Acronym letter-spellings (`LLM -> L L M`, ~147 seed rows; the deterministic
  `_normalize_acronyms` speller).
- Real-word swaps (`SQL -> sequel`, `GIF -> jif`, `RAM -> ram`, `GUI -> gooey`).
- Homograph disambiguation (`read -> reed`, `lead -> led`) and the LLM pronunciation
  pass that applies them by context.
- The entire base lexicon and the user correction dictionary.

## Propagation

The seed rows on an existing database came from the one-time `_m012` migration, not the
shipped base artifact (which contains zero seed-origin hyphenated rows). So a CSV edit
alone reaches only fresh installs. To update existing databases (including production):

- Add a new sequential DB migration that re-imports the trimmed seed CSV via
  `lexicon.import_readonly(conn, "seed", rows)`, building rows with the same logic
  `_m012` uses (`seed_corrections.load_seed` + `pronounce_convert.classify_mode` /
  `default_case_sensitive` / `CONF_CURATED`). `import_readonly` replaces only
  `origin='seed'` rows, so user corrections and base rows are untouched.

No base-lexicon artifact rebuild and no lexicon-version bump are required.

## Accepted trade-offs

- Acronyms that were read-as-word (`YAML`, `JSON`, `ASCII`, `BIOS`) now either
  letter-spell (`Y A M L`) via the acronym speller, or, when they are in the
  `_ACRONYM_KEEP` set, pass through for Chatterbox's native guess.
- Brand/product names (`Nike`, `Porsche`, `Kubernetes`, `Hyundai`) use Chatterbox's
  native pronunciation. The operator prefers native over choppy.

## Testing

Update/remove:
- Remove `test_normalize_months`.
- Remove `test_corrections_folds_emphasis_caps_in_respelling` (A3 fold gone).
- Update `test_normalize_for_tts_strips_headings_and_expands_dates`: `Jan 15` now
  yields `January 15`, not `jan-yoo-air-ee 15`.
- Update `test_corrections_applies_seed_brand_phrase`: `Louis Vuitton -> loo-ee
  vwee-tohn` is a deleted respelling; reassert a kept correction instead.

Add:
- A former respell word passes through unchanged (e.g. `pseudonym`, `Kubernetes`).
- Kept corrections still apply: `LLM -> L L M`, `SQL -> sequel`.

## Verification

- `uv run pytest` green; `uv run ruff check` clean; frontend builds (no frontend change).
- On a scratch DB: run migrations, confirm the seed lexicon no longer contains the
  removed rows and still contains the kept ones.
- End to end: process the 404 Media article (which earlier produced `par-TIK-yoo-ler-lee`
  and `LLMs`) and confirm `particularly` is now plain and `LLMs` still spells correctly.

## Out of scope / follow-ups

- Versioning the seed re-import (so future seed edits propagate without a new migration)
  is a separate improvement; this change ships one migration for the deletion.
- Base lexicon untouched.
