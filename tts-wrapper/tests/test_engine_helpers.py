"""Tests for the shared split/encode helpers in engine.py.

These cover the text-splitting logic the engine reuses; they don't import torch
or the TTS model library.
"""

from __future__ import annotations

from engine import _DEFAULT_MAX_CHARS, _split_into_pieces


def test_split_short_text_one_piece_per_sentence() -> None:
    pieces = _split_into_pieces("A short sentence. Another short one.")
    assert pieces == ["A short sentence.", "Another short one."]


def test_split_empty_and_whitespace() -> None:
    assert _split_into_pieces("") == []
    assert _split_into_pieces("   ") == []


def test_split_keeps_every_piece_under_cap() -> None:
    # ~1000 chars, no sentence breaks -> must be cut into <=cap pieces.
    long = ("word " * 200).strip() + "."
    pieces = _split_into_pieces(long)
    assert pieces
    assert all(len(p) <= _DEFAULT_MAX_CHARS for p in pieces)
    # No content dropped: every "word" survives across the pieces.
    assert "".join(pieces).count("word") == long.count("word")


def test_split_oversize_sentence_cut_at_word_boundary() -> None:
    sentence = ("alpha " * 60).strip() + "."  # ~360 chars, single sentence, no clauses
    pieces = _split_into_pieces(sentence)
    assert len(pieces) >= 2
    assert all(len(p) <= _DEFAULT_MAX_CHARS for p in pieces)
    # No clause break to cut on, so cuts fall on spaces -- never mid-"alpha".
    assert all(not p.endswith("alph") for p in pieces)


def test_split_collapses_whitespace_and_newlines() -> None:
    # Stray newlines (Chatterbox reads them as ~0.1s pauses), tabs, and runs of spaces
    # collapse to single spaces, so a piece never carries incidental markdown whitespace.
    pieces = _split_into_pieces("First line.\n\nSecond  line\twith   gaps.")
    assert pieces == ["First line.", "Second line with gaps."]


def test_split_oversize_sentence_cut_at_clause_boundary() -> None:
    # A long single sentence with commas cuts AFTER a comma (a natural pause), so the
    # silence gap lands on the clause break rather than mid-clause.
    sentence = ("the quick brown fox jumped over the lazy dog, " * 8).strip()
    pieces = _split_into_pieces(sentence)
    assert len(pieces) >= 2
    assert all(len(p) <= _DEFAULT_MAX_CHARS for p in pieces)
    # Every non-final piece ends on the clause punctuation it was cut at.
    assert all(p.endswith(",") for p in pieces[:-1])
