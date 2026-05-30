from __future__ import annotations

import logging
from pathlib import Path

import pytest
from app.config import get_settings
from app.services import chunker


def _sentences(count: int, prefix: str = "Sentence body.") -> str:
    return " ".join(f"{prefix}" for _ in range(count))


def test_chunk_empty_returns_empty(env: Path) -> None:
    assert chunker.chunk("", get_settings()) == []


def test_pack_paragraphs_empty_returns_empty() -> None:
    assert chunker.pack_paragraphs("", 1000) == []


def test_pack_paragraphs_groups_under_limit() -> None:
    # Three ~100-char paragraphs into a 250-char window: 2 + 1.
    paras = ["A" * 100, "B" * 100, "C" * 100]
    windows = chunker.pack_paragraphs("\n\n".join(paras), 250)
    assert len(windows) == 2
    assert windows[0] == f"{'A' * 100}\n\n{'B' * 100}"
    assert windows[1] == "C" * 100
    # No content lost: every paragraph survives across the windows.
    joined = "\n\n".join(windows)
    for para in paras:
        assert para in joined


def test_pack_paragraphs_oversize_paragraph_is_own_window() -> None:
    big = "X" * 5000
    windows = chunker.pack_paragraphs(f"{big}\n\nsmall", 1000)
    assert windows[0] == big
    assert windows[1] == "small"


def test_chunk_single_short_paragraph_returns_one_chunk(env: Path) -> None:
    text = "One short sentence. Another short sentence."
    result = chunker.chunk(text, get_settings())
    assert result == [text]


def test_chunk_splits_on_paragraph_boundaries(env: Path) -> None:
    text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
    result = chunker.chunk(text, get_settings())
    assert result == ["Paragraph one.", "Paragraph two.", "Paragraph three."]


def test_chunk_greedy_packs_sentences_into_target(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Greedy packing keeps each chunk near target_words; sentences flow into
    the same chunk until adding the next would exceed target."""

    monkeypatch.setenv("TTS_CHUNK_TARGET_WORDS", "8")
    monkeypatch.setenv("TTS_CHUNK_MAX_WORDS", "12")
    get_settings.cache_clear()

    # Each sentence is ~3 words. Target=8 => 2 sentences per chunk (6 words);
    # 3 would be 9 > 8.
    text = (
        "First sentence here. "
        "Second sentence here. "
        "Third sentence here. "
        "Fourth sentence here. "
        "Fifth sentence here."
    )
    result = chunker.chunk(text, get_settings())
    assert len(result) == 3
    for chunk in result:
        assert len(chunk.split()) <= 12


def test_chunk_falls_back_to_comma_split_on_long_sentence(
    env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("TTS_CHUNK_TARGET_WORDS", "8")
    monkeypatch.setenv("TTS_CHUNK_MAX_WORDS", "10")
    get_settings.cache_clear()

    # 15-word sentence with commas in it.
    text = (
        "This is a very long single sentence that, "
        "with several embedded clauses, "
        "exceeds the configured maximum word count."
    )

    with caplog.at_level(logging.WARNING, logger="app.services.chunker"):
        result = chunker.chunk(text, get_settings())

    assert any(rec.message.startswith("Chunk fallback") for rec in caplog.records), [
        (r.name, r.message) for r in caplog.records
    ]
    assert len(result) >= 2
    for chunk in result:
        assert len(chunk.split()) <= 10


def test_chunk_hard_aborts_on_unsplittable_sentence(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sentence with no commas/semicolons that exceeds max_words has no
    safe breakpoint; chunker raises rather than truncate or force-split."""

    monkeypatch.setenv("TTS_CHUNK_TARGET_WORDS", "8")
    monkeypatch.setenv("TTS_CHUNK_MAX_WORDS", "10")
    get_settings.cache_clear()

    text = (
        "this sentence has fifteen words but no commas to allow a safe fallback breakpoint anywhere"
    )

    with pytest.raises(chunker.UnsplittableSentenceError) as exc_info:
        chunker.chunk(text, get_settings())
    assert exc_info.value.word_count >= 10
    assert "this sentence has" in exc_info.value.sentence_preview


def test_chunk_char_cap_overrides_word_count(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A paragraph with a tiny word count but pathologically long characters
    (e.g. a 1000-char URL) must still trigger a split."""

    monkeypatch.setenv("TTS_CHUNK_TARGET_WORDS", "1000")
    monkeypatch.setenv("TTS_CHUNK_MAX_WORDS", "1000")
    monkeypatch.setenv("TTS_CHUNK_MAX_CHARS", "60")
    get_settings.cache_clear()

    text = "Short start. " + "x" * 200 + " end."
    with pytest.raises(chunker.UnsplittableSentenceError):
        chunker.chunk(text, get_settings())


def test_chunk_normalizes_repeated_blank_lines(env: Path) -> None:
    text = "Paragraph one.\n\n\n\nParagraph two."
    result = chunker.chunk(text, get_settings())
    assert result == ["Paragraph one.", "Paragraph two."]


def test_chunk_preserves_sentence_punctuation(env: Path) -> None:
    text = "Is this a question? Yes! And this is a statement."
    result = chunker.chunk(text, get_settings())
    assert "Is this a question?" in result[0]
    assert "Yes!" in result[0]


def test_chunk_preserves_semicolons_in_comma_fallback(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Comma/semicolon fallback must keep the original separator. XTTS-v2
    treats commas and semicolons with different pause prosody; silently
    rewriting semicolons to commas would change the narrator's pace."""

    monkeypatch.setenv("TTS_CHUNK_TARGET_WORDS", "30")
    monkeypatch.setenv("TTS_CHUNK_MAX_WORDS", "10")
    get_settings.cache_clear()

    text = "Alpha alpha alpha alpha alpha, beta beta beta beta beta; gamma gamma gamma gamma gamma."
    result = chunker.chunk(text, get_settings())
    rejoined = " ".join(result)
    assert ";" in rejoined, rejoined


def test_chunk_fallback_warn_log_has_global_chunk_index(
    env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """chunk_index in chunk_fallback_split WARN records must be the running
    article-wide chunk position, not a per-paragraph 0-reset."""

    monkeypatch.setenv("TTS_CHUNK_TARGET_WORDS", "8")
    monkeypatch.setenv("TTS_CHUNK_MAX_WORDS", "10")
    get_settings.cache_clear()

    # Two paragraphs each triggering comma fallback; the second's WARN must
    # have a chunk_index > 0.
    # Sentences too long for the chunk limits but each clause < max_words
    text = (
        "alpha alpha alpha alpha alpha, beta beta beta beta beta, "
        "gamma gamma gamma gamma.\n\n"
        "delta delta delta delta delta, epsilon epsilon epsilon epsilon epsilon, "
        "zeta zeta zeta zeta."
    )
    with caplog.at_level(logging.WARNING, logger="app.services.chunker"):
        chunker.chunk(text, get_settings())

    fallback_records = [
        rec for rec in caplog.records if getattr(rec, "event", "") == "chunk_fallback_split"
    ]
    assert len(fallback_records) >= 2
    chunk_indices = [rec.chunk_index for rec in fallback_records]
    assert chunk_indices[1] > 0, f"second WARN should have chunk_index>0: {chunk_indices}"
