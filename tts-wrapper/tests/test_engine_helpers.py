"""Tests for the shared split/encode helpers in engine.py.

These cover the text-splitting logic the engine reuses; they don't import torch
or the TTS model library.
"""

from __future__ import annotations

import numpy as np

from engine import (
    _ALL_SILENT_KEEP_MS,
    _DEFAULT_MAX_CHARS,
    _INTERNAL_SILENCE_KEEP_MS,
    _TRIM_BUFFER_MS,
    _split_into_pieces,
    compress_internal_silence_np,
    trim_edge_silence,
)


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


# --- trim_edge_silence ------------------------------------------------------


def _speech(secs: float, rate: int = 24000, level: float = 0.5) -> "np.ndarray":
    return np.full(int(rate * secs), level, dtype=np.float32)


def _silence(secs: float, rate: int = 24000) -> "np.ndarray":
    return np.zeros(int(rate * secs), dtype=np.float32)


def test_trim_removes_leading_and_trailing_silence() -> None:
    rate = 24000
    wav = np.concatenate([_silence(2.0), _speech(1.0), _silence(30.0)])
    out = trim_edge_silence(wav, rate)
    # Speech body plus at most the buffer on each side; the 30s run-to-cap
    # tail (the EOS-failure signature) must be gone.
    buffer_n = int(rate * _TRIM_BUFFER_MS / 1000)
    assert len(out) == int(rate * 1.0) + 2 * buffer_n
    # The kept region is centered on the speech: its loud part is intact.
    assert np.abs(out).max() == np.float32(0.5)
    assert np.count_nonzero(np.abs(out) > 0.003) == int(rate * 1.0)


def test_trim_keeps_buffer_samples_on_both_ends() -> None:
    rate = 24000
    wav = np.concatenate([_silence(1.0), _speech(0.5), _silence(1.0)])
    out = trim_edge_silence(wav, rate)
    buffer_n = int(rate * _TRIM_BUFFER_MS / 1000)
    # Leading and trailing buffer regions are silence carried from the source.
    assert np.abs(out[:buffer_n]).max() <= 0.003
    assert np.abs(out[-buffer_n:]).max() <= 0.003


def test_trim_leaves_piece_without_edge_silence_unchanged() -> None:
    rate = 24000
    wav = _speech(0.8)
    out = trim_edge_silence(wav, rate)
    assert len(out) == len(wav)
    assert np.array_equal(out, wav)


def test_trim_all_silent_piece_returns_short_remnant() -> None:
    rate = 24000
    wav = _silence(40.0)  # a full run-to-cap silent generation
    out = trim_edge_silence(wav, rate)
    # Kept short so ASR verify flags the dropped text instead of the episode
    # carrying 40s of dead air.
    assert len(out) == int(rate * _ALL_SILENT_KEEP_MS / 1000)
    assert np.abs(out).max() <= 0.003


def test_trim_short_piece_not_erased() -> None:
    rate = 24000
    wav = _silence(0.1)  # shorter than the all-silent remnant
    out = trim_edge_silence(wav, rate)
    assert len(out) == len(wav)


# --- compress_internal_silence_np -------------------------------------------


def test_internal_silence_run_compressed_to_keep_length() -> None:
    rate = 24000
    wav = np.concatenate([_speech(1.0), _silence(10.0), _speech(1.0)])
    out = compress_internal_silence_np(wav, rate)
    keep = int(rate * _INTERNAL_SILENCE_KEEP_MS / 1000)
    assert len(out) == int(rate * 2.0) + keep
    # Speech on both sides intact.
    assert np.count_nonzero(np.abs(out) > 0.003) == int(rate * 2.0)


def test_internal_silence_under_cap_untouched() -> None:
    rate = 24000
    wav = np.concatenate([_speech(0.5), _silence(0.8), _speech(0.5)])
    out = compress_internal_silence_np(wav, rate)
    assert len(out) == len(wav)
    assert np.array_equal(out, wav)


def test_internal_silence_multiple_runs_all_compressed() -> None:
    rate = 24000
    wav = np.concatenate(
        [_speech(0.5), _silence(3.0), _speech(0.5), _silence(5.0), _speech(0.5)]
    )
    out = compress_internal_silence_np(wav, rate)
    keep = int(rate * _INTERNAL_SILENCE_KEEP_MS / 1000)
    assert len(out) == int(rate * 1.5) + 2 * keep


def test_internal_silence_all_silent_untouched() -> None:
    rate = 24000
    wav = _silence(2.0)
    out = compress_internal_silence_np(wav, rate)
    assert np.array_equal(out, wav)
