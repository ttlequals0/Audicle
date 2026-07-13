from __future__ import annotations

from app.services import asr_verify


def _div(expected: str, transcript: str) -> float:
    return asr_verify.analyze(expected, transcript)[0]


def _run(expected: str, transcript: str) -> int:
    return asr_verify.analyze(expected, transcript)[1]


def test_divergence_identical_is_zero() -> None:
    text = "The quarterly report covered inflation and the labor market."
    assert _div(text, text) == 0.0


def test_divergence_ignores_case_and_punctuation() -> None:
    expected = "The Economy grew, slowly, last year!"
    transcript = "the economy grew slowly last year"
    assert _div(expected, transcript) == 0.0


def test_divergence_high_on_dropped_tail() -> None:
    expected = " ".join(f"word{i}" for i in range(40))
    transcript = " ".join(f"word{i}" for i in range(10))  # 75% dropped
    assert _div(expected, transcript) > 0.4


def test_divergence_high_on_leaked_preamble() -> None:
    expected = "Markets fell sharply on the news."
    transcript = (
        "Sure, here is the cleaned narration you asked for. Markets fell sharply on the news."
    )
    assert _div(expected, transcript) > 0.2


def test_divergence_total_dropout_is_one() -> None:
    assert _div("some expected words here", "") == 1.0


def test_divergence_both_empty_is_zero() -> None:
    assert _div("", "") == 0.0


def test_normalize_words_keeps_internal_apostrophes() -> None:
    assert asr_verify.normalize_words("Don't stop, it's fine.") == ["don't", "stop", "it's", "fine"]


def test_normalize_words_collapses_spaced_acronyms() -> None:
    # _normalize_dotted_acronyms feeds TTS spaced letters ("F O R T R A N");
    # ASR hears the joined word, so compare them joined.
    assert asr_verify.normalize_words("F O R T R A N") == ["fortran"]
    assert asr_verify.normalize_words("the B N F report") == ["the", "bnf", "report"]


def test_normalize_words_collapses_two_letter_acronyms() -> None:
    # _normalize_dotted_acronyms spaces acronyms of 2+ letters ("A.I." -> "A I",
    # "U.S." -> "U S"); ASR hears the joined word, so 2-letter runs join too.
    assert asr_verify.normalize_words("the U S economy") == ["the", "us", "economy"]
    assert _div("A I will change the U S economy", "AI will change the US economy") == 0.0


def test_normalize_words_keeps_isolated_single_letter_words() -> None:
    # Isolated "a"/"I" are runs of 1 -- legitimate prose, left alone.
    assert asr_verify.normalize_words("I saw a bird") == ["i", "saw", "a", "bird"]


def test_normalize_words_keeps_single_digits() -> None:
    # The acronym normalizer only ever emits letters; digit lists ("scores
    # 7 8 9") are real content and must not fuse into one token.
    assert asr_verify.normalize_words("scores 7 8 9") == ["scores", "7", "8", "9"]


def test_worst_run_catches_localized_garbage_that_passes_global() -> None:
    # A 15-word babble span inside a 100-word chunk: global divergence stays
    # under the 0.35 threshold (the historical blind spot) but the run is long.
    expected = " ".join(f"word{i}" for i in range(100))
    garbled = [f"word{i}" for i in range(100)]
    garbled[50:65] = [f"garble{i}" for i in range(15)]
    transcript = " ".join(garbled)
    assert _div(expected, transcript) < 0.35
    assert _run(expected, transcript) >= 8


def test_worst_run_catches_contiguous_dropout() -> None:
    expected = " ".join(f"word{i}" for i in range(60))
    transcript = " ".join(f"word{i}" for i in range(60) if not 40 <= i < 53)
    assert _run(expected, transcript) == 13


def test_worst_run_merges_garbage_bridged_by_accidental_matches() -> None:
    # Garbage runs often contain a stray real word ("the", "and") that matches
    # by accident; a 1-2 word equal block must not split the run.
    expected = " ".join(f"word{i}" for i in range(40))
    garbled = (
        [f"word{i}" for i in range(20)]
        + ["junk0", "junk1", "junk2", "junk3", "word24", "junk4", "junk5", "junk6", "junk7"]
        + [f"word{i}" for i in range(29, 40)]
    )
    assert _run(expected, " ".join(garbled)) >= 8


def test_worst_run_small_for_scattered_mishears() -> None:
    # Proper-noun mishears (2-3 words) separated by clean stretches must stay
    # well under the run threshold.
    expected = " ".join(f"word{i}" for i in range(40))
    garbled = [f"word{i}" for i in range(40)]
    garbled[5:7] = ["von", "winggarden"]
    garbled[25:27] = ["fore", "clothes"]
    assert _run(expected, " ".join(garbled)) == 2


def test_worst_run_excludes_edge_equal_blocks() -> None:
    # A short equal block ADJACENT to a mismatch is not part of the divergent
    # stretch; only diverging words count toward the run length.
    expected = " ".join(f"word{i}" for i in range(30))
    garbled = [f"word{i}" for i in range(30)]
    garbled[2:8] = [f"garble{i}" for i in range(6)]  # equal(2), replace(6), equal(22)
    assert _run(expected, " ".join(garbled)) == 6


def test_worst_run_does_not_chain_scattered_single_mishears() -> None:
    # ASR mishearing isolated words rhythmically (every 3rd word: jargon-dense
    # or name-dense text on perfectly fine audio) must not fuse into one long
    # run via the bridge rule.
    expected = " ".join(f"word{i}" for i in range(30))
    garbled = [f"word{i}" for i in range(30)]
    for i in range(2, 30, 3):
        garbled[i] = f"mishear{i}"
    assert _run(expected, " ".join(garbled)) == 1


def test_worst_run_total_dropout_is_full_length() -> None:
    assert _run("expected words here now", "") == 4


def test_worst_run_identical_is_zero() -> None:
    text = "the quarterly report covered inflation and the labor market"
    assert _run(text, text) == 0
    assert _run("", "") == 0
    assert _run("two words", "two words") == 0
