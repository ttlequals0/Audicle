# Third-party notices

The bundled base-lexicon artifact (`backend/app/data/base_lexicon.jsonl.gz`),
built by `scripts/build_base_lexicon.py`, is derived from the following sources.
Each entry records the source's license and the attribution it requires.

## ISLEX (via pysle)
- Project: https://github.com/timmahrt/pysle
- License: MIT
- Use: word -> IPA pronunciations (the bulk of the base lexicon).

## CMU Pronouncing Dictionary
- Project: https://github.com/words/cmu-pronouncing-dictionary
- License: ISC
- Use: word -> ARPAbet pronunciations, converted to IPA.

## USA Cities and States
- Project: https://github.com/grammakov/USA-cities-and-states
- License: CC0-1.0 (public domain)
- Use: US city/place names.

## Countries States Cities Database
- Project: https://github.com/dr5hn/countries-states-cities-database
- License: Open Database License (ODbL) v1.0
- Use: international city/place names.
- ODbL is share-alike: any publicly distributed derived database that includes
  this data is licensed under ODbL and must attribute the source. The
  base-lexicon artifact contains city names derived from this database; this
  notice provides the required attribution.

## wiki-pronunciation-dict (Wiktionary-derived)
- Project: https://github.com/DanielSWolf/wiki-pronunciation-dict
- Code license: MIT. Data license: the pronunciations are extracted from
  Wiktionary, whose text is CC BY-SA 3.0. The bundled artifact includes
  Wiktionary-derived IPA; this notice attributes Wiktionary
  (https://www.wiktionary.org) under CC BY-SA 3.0.

## balacoon en_us_abbreviations
- Dataset: https://huggingface.co/datasets/balacoon/en_us_abbreviations
- Use: abbreviation spell-out lists and read-as-word token lists.
- Confirmed acceptable for use; see the dataset card for terms.

## Google WikipediaAbbreviationData
- Project: https://github.com/google-research-datasets/WikipediaAbbreviationData
- License: Apache-2.0
- Use: abbreviation -> expansion pairs (ingested but applied below the
  base-lexicon confidence gate, as the crowd data is noisy).
