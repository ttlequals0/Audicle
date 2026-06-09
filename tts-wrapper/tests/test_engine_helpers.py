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
    sentence = ("alpha " * 60).strip() + "."  # ~360 chars, single sentence
    pieces = _split_into_pieces(sentence)
    assert len(pieces) >= 2
    assert all(len(p) <= _DEFAULT_MAX_CHARS for p in pieces)
    # Cuts happen at spaces, so no piece ends mid-"alpha".
    assert all(not p.endswith("alph") for p in pieces)
