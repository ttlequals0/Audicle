"""Text-level divergence between expected narration and an ASR transcript.

When ASR verification is enabled, the tts-wrapper transcribes each produced
chunk with faster-whisper and the pipeline compares that transcript against the
text we asked it to speak. A high divergence means the audio does not say what
it should -- dropped content, a repeated/hallucinated run, or a leaked cleanup
preamble -- which the quality loop treats as a regen trigger.

The metrics are intentionally simple and dependency-free: normalize both sides
to lowercase whitespace-separated word lists (punctuation dropped, since ASR
rarely matches the source punctuation) and diff the word sequences with
``SequenceMatcher``. Two signals come out of one diff: the global divergence
(``1 - ratio``, 0.0 identical .. 1.0 nothing in common) and the worst divergent
run (longest contiguous stretch of diverging words). The global ratio catches
whole-chunk failures but dilutes localized garbage -- 15 bad words inside a
100-word chunk score ~0.15 -- which is exactly what the run length measures.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from itertools import groupby

# Keep word-internal apostrophes (don't, it's) but drop every other punctuation
# mark so "U.S." vs "us" and "end." vs "end" do not register as differences.
_NON_WORD = re.compile(r"[^\w'\s]")

# Runs of this many-or-more single-character words are spaced acronyms from TTS
# normalization ("F O R T R A N", "A I" -- pipeline._normalize_dotted_acronyms
# spaces acronyms of 2+ letters); ASR hears the joined word, so join them for
# comparison. Isolated "a"/"I" prose words are runs of 1 and stay untouched;
# accidental prose adjacency ("plan B I guess") collapses identically on both
# sides, so it introduces no divergence.
_ACRONYM_MIN_RUN = 2

# An equal block this short between two mismatch blocks is an accidental match
# (a stray "the" or "and" inside babble) and does not split a divergent run.
_RUN_BRIDGE_WORDS = 2

# ...but only when the mismatch on at least one side of the bridge is this
# big. Two isolated one-word mishears around an accidental match are ASR noise
# (rhythmic name/jargon mishears on fine audio), not one garbage stretch.
_RUN_MERGE_MIN_BLOCK = 2


def _collapse_single_letter_runs(words: list[str]) -> list[str]:
    out: list[str] = []
    # isalpha: the acronym normalizer only emits letters; digit lists
    # ("scores 7 8 9") are real content and must not fuse.
    for is_single, group in groupby(words, key=lambda w: len(w) == 1 and w.isalpha()):
        run = list(group)
        if is_single and len(run) >= _ACRONYM_MIN_RUN:
            out.append("".join(run))
        else:
            out.extend(run)
    return out


def normalize_words(text: str) -> list[str]:
    """Lowercase, strip punctuation, and split into a word list for comparison."""

    cleaned = _NON_WORD.sub(" ", text.lower())
    return _collapse_single_letter_runs(cleaned.split())


def analyze(expected: str, transcript: str) -> tuple[float, int]:
    """Return ``(divergence, worst_divergent_run)`` from a single word diff.

    Divergence is ``1 - SequenceMatcher.ratio`` over the normalized word lists:
    0.0 for identical wording (including both sides empty), 1.0 when nothing
    matches (including an empty transcript against expected text -- total
    dropout). The worst run counts the diverging words of the longest garbage
    stretch: an equal block of <= _RUN_BRIDGE_WORDS words joins the mismatch
    blocks around it (without itself counting) when at least one of them has
    >= _RUN_MERGE_MIN_BLOCK words, and a run's length is the max of its
    expected-side and transcript-side word counts so dropout (delete) and
    inserted babble (insert) both count.
    """

    expected_words = normalize_words(expected)
    transcript_words = normalize_words(transcript)
    # autojunk=False: the default heuristic marks any word occurring in >1% of a
    # sequence longer than 200 items as "junk" and skips it when finding matching
    # blocks. Chunks routinely exceed 200 words and common words (the, and, of)
    # clear that bar, which fragments the match and inflates divergence -- a
    # single changed word in a 250-word chunk scored ~0.6 with it on. Off, the
    # ratio is a faithful word-level similarity regardless of chunk length.
    matcher = SequenceMatcher(None, expected_words, transcript_words, autojunk=False)
    worst = run_exp = run_tr = 0
    last_block = 0  # size of the current run's most recent mismatch block
    bridged = False  # a short equal block sits between the run and the next mismatch
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        exp_len, tr_len = i2 - i1, j2 - j1
        if tag == "equal":
            bridged = bool(run_exp or run_tr) and exp_len <= _RUN_BRIDGE_WORDS
            if not bridged:
                worst = max(worst, run_exp, run_tr)
                run_exp = run_tr = last_block = 0
            continue
        size = max(exp_len, tr_len)
        if bridged and max(last_block, size) < _RUN_MERGE_MIN_BLOCK:
            worst = max(worst, run_exp, run_tr)
            run_exp = run_tr = 0
        run_exp += exp_len
        run_tr += tr_len
        last_block = size
        bridged = False
    return 1.0 - matcher.ratio(), max(worst, run_exp, run_tr)
