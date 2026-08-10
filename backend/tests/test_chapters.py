"""Chapter generation service (3b): LLM line parsing, deterministic timestamp
math from chunk durations, and the Podcasting 2.0 chapters JSON document."""

from __future__ import annotations

import json

from app.services import chapters


def test_chunk_start_times_sum_durations_and_silence() -> None:
    starts = chapters.chunk_start_times([10.0, 20.0, 30.0], silence_ms=500)
    assert starts == [0.0, 10.5, 31.0]


def test_parse_llm_chapters_reads_index_pipe_title_lines() -> None:
    raw = "0 | Opening remarks\n4 | The core argument\n9 | Where it lands\n"
    assert chapters.parse_llm_chapters(raw, chunk_count=12) == [
        (0, "Opening remarks"),
        (4, "The core argument"),
        (9, "Where it lands"),
    ]


def test_parse_llm_chapters_ignores_junk_and_out_of_range() -> None:
    raw = (
        "Here are the chapters:\n"
        "- 0 | Opening\n"
        "99 | Beyond the end\n"
        "not a line\n"
        "3 | Valid one\n"
        "3 | Duplicate index ignored\n"
    )
    assert chapters.parse_llm_chapters(raw, chunk_count=10) == [
        (0, "Opening"),
        (3, "Valid one"),
    ]


def test_parse_llm_chapters_forces_chapter_at_zero() -> None:
    assert chapters.parse_llm_chapters("5 | Late start", chunk_count=10) == [
        (0, "Introduction"),
        (5, "Late start"),
    ]


def test_parse_llm_chapters_empty_reply_returns_empty() -> None:
    assert chapters.parse_llm_chapters("no chapters here", chunk_count=10) == []


def test_build_chapters_json_document() -> None:
    doc = json.loads(
        chapters.build_chapters_json(
            [(0, "Opening"), (2, "Middle")], starts=[0.0, 10.5, 31.0]
        )
    )
    assert doc["version"] == "1.2.0"
    assert doc["chapters"] == [
        {"startTime": 0.0, "title": "Opening"},
        {"startTime": 31.0, "title": "Middle"},
    ]


def test_embed_chapters_writes_chap_and_ctoc_frames(env, tmp_path) -> None:
    import numpy as np
    import soundfile as sf
    from app.config import get_settings
    from app.services import audio
    from mutagen.id3 import ID3

    src_wav = tmp_path / "in.wav"
    t = np.arange(24000 * 2) / 24000
    sf.write(str(src_wav), (0.3 * np.sin(2 * np.pi * 220 * t)).astype("float32"), 24000)
    mp3 = tmp_path / "out.mp3"
    audio.normalize_and_encode(src_wav, mp3, get_settings())

    audio.embed_chapters(mp3, [(0.0, "One"), (1.0, "Two")], total_duration_secs=2.0)

    tags = ID3(str(mp3))
    chaps = tags.getall("CHAP")
    assert len(chaps) == 2
    assert chaps[0].start_time == 0 and chaps[0].end_time == 1000
    assert chaps[1].start_time == 1000 and chaps[1].end_time == 2000
    ctoc = tags.getall("CTOC")
    assert len(ctoc) == 1
    assert list(ctoc[0].child_element_ids) == [c.element_id for c in chaps]
