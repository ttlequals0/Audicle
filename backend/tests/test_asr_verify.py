from __future__ import annotations

from app.services import asr_verify


def test_divergence_identical_is_zero() -> None:
    text = "The quarterly report covered inflation and the labor market."
    assert asr_verify.divergence(text, text) == 0.0


def test_divergence_ignores_case_and_punctuation() -> None:
    expected = "The Economy grew, slowly, last year!"
    transcript = "the economy grew slowly last year"
    assert asr_verify.divergence(expected, transcript) == 0.0


def test_divergence_high_on_dropped_tail() -> None:
    expected = " ".join(f"word{i}" for i in range(40))
    transcript = " ".join(f"word{i}" for i in range(10))  # 75% dropped
    assert asr_verify.divergence(expected, transcript) > 0.4


def test_divergence_high_on_leaked_preamble() -> None:
    expected = "Markets fell sharply on the news."
    transcript = (
        "Sure, here is the cleaned narration you asked for. Markets fell sharply on the news."
    )
    assert asr_verify.divergence(expected, transcript) > 0.2


def test_divergence_total_dropout_is_one() -> None:
    assert asr_verify.divergence("some expected words here", "") == 1.0


def test_divergence_both_empty_is_zero() -> None:
    assert asr_verify.divergence("", "") == 0.0


def test_normalize_words_keeps_internal_apostrophes() -> None:
    assert asr_verify.normalize_words("Don't stop, it's fine.") == ["don't", "stop", "it's", "fine"]
