from __future__ import annotations

import numpy as np
from config import Config
from engine import _XTTS_MAX_CHARS, XTTSEngine, _split_for_xtts


def test_run_inference_whitespace_text_returns_silence() -> None:
    # A whitespace-only chunk has no speakable pieces; _run_inference must return
    # brief silence rather than feeding "" to XTTS (which crashes inference).
    engine = XTTSEngine(Config.from_env())
    engine._model = object()  # sentinel: the empty path returns before using it
    engine.sample_rate = 24000
    out = engine._run_inference("   ")
    assert isinstance(out, np.ndarray)
    assert out.dtype == np.float32
    assert len(out) > 0
    assert not out.any()  # all zeros


def test_split_short_text_one_piece_per_sentence() -> None:
    pieces = _split_for_xtts("A short sentence. Another short one.")
    assert pieces == ["A short sentence.", "Another short one."]


def test_split_empty_and_whitespace() -> None:
    assert _split_for_xtts("") == []
    assert _split_for_xtts("   ") == []


def test_split_keeps_every_piece_under_cap() -> None:
    # ~1000 chars, no sentence breaks -> must be cut into <=cap pieces.
    long = ("word " * 200).strip() + "."
    pieces = _split_for_xtts(long)
    assert pieces
    assert all(len(p) <= _XTTS_MAX_CHARS for p in pieces)
    # No content dropped: every "word" survives across the pieces.
    assert "".join(pieces).count("word") == long.count("word")


def test_split_oversize_sentence_cut_at_word_boundary() -> None:
    sentence = ("alpha " * 60).strip() + "."  # ~360 chars, single sentence
    pieces = _split_for_xtts(sentence)
    assert len(pieces) >= 2
    assert all(len(p) <= _XTTS_MAX_CHARS for p in pieces)
    # Cuts happen at spaces, so no piece ends mid-"alpha".
    assert all(not p.endswith("alph") for p in pieces)
