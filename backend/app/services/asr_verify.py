"""Text-level divergence between expected narration and an ASR transcript.

When ASR verification is enabled, the tts-wrapper transcribes each produced
chunk with faster-whisper and the pipeline compares that transcript against the
text we asked it to speak. A high divergence means the audio does not say what
it should -- dropped content, a repeated/hallucinated run, or a leaked cleanup
preamble -- which the quality loop treats as a regen trigger.

The metric is intentionally simple and dependency-free: normalize both sides to
lowercase whitespace-separated word lists (punctuation dropped, since ASR rarely
matches the source punctuation) and take ``1 - SequenceMatcher.ratio`` over the
word sequences. 0.0 means identical wording; 1.0 means nothing in common. This
is a similarity gap, not a true word error rate, but it is monotonic in the
failures we care about and needs no model on the backend.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# Keep word-internal apostrophes (don't, it's) but drop every other punctuation
# mark so "U.S." vs "us" and "end." vs "end" do not register as differences.
_NON_WORD = re.compile(r"[^\w'\s]")


def normalize_words(text: str) -> list[str]:
    """Lowercase, strip punctuation, and split into a word list for comparison."""

    cleaned = _NON_WORD.sub(" ", text.lower())
    return cleaned.split()


def divergence(expected: str, transcript: str) -> float:
    """Return a 0..1 word-level divergence between ``expected`` and ``transcript``.

    Returns 1.0 when the transcript is empty but text was expected (total
    dropout) and 0.0 when both are empty (nothing to compare).
    """

    expected_words = normalize_words(expected)
    transcript_words = normalize_words(transcript)
    if not expected_words and not transcript_words:
        return 0.0
    if not expected_words or not transcript_words:
        return 1.0
    # autojunk=False: the default heuristic marks any word occurring in >1% of a
    # sequence longer than 200 items as "junk" and skips it when finding matching
    # blocks. Chunks routinely exceed 200 words and common words (the, and, of)
    # clear that bar, which fragments the match and inflates divergence -- a
    # single changed word in a 250-word chunk scored ~0.6 with it on. Off, the
    # ratio is a faithful word-level similarity regardless of chunk length.
    ratio = SequenceMatcher(None, expected_words, transcript_words, autojunk=False).ratio()
    return 1.0 - ratio
