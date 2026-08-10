"""Chapter generation (3b): timestamped-transcript prompt input, tolerant
parsing of the LLM's ``MM:SS Title`` reply, and the Podcasting 2.0 document."""

from __future__ import annotations

import json

from app.services import chapters, transcript


def test_chunk_start_ms_sums_durations_and_silence() -> None:
    starts = transcript.chunk_start_ms([10.0, 20.0, 30.0], silence_ms=500)
    assert starts == [0, 10500, 31000]


# --- prompt input -----------------------------------------------------------


def test_timestamped_transcript_marks_each_line() -> None:
    text = chapters.timestamped_transcript(
        [(0.0, "Opening words here."), (65.0, "The middle bit."), (3725.0, "Late on.")]
    )
    assert text.splitlines() == [
        "[00:00] Opening words here.",
        "[01:05] The middle bit.",
        "[62:05] Late on.",
    ]


def test_timestamped_transcript_trims_long_lines() -> None:
    text = chapters.timestamped_transcript([(0.0, "word " * 200)])
    assert len(text) < 400


# --- reply parsing ----------------------------------------------------------


def test_parse_reads_mm_ss_lines() -> None:
    raw = "00:00 Opening remarks\n05:30 The core argument\n12:45 Where it lands"
    assert chapters.parse_llm_chapters(raw, duration_secs=1800) == [
        (0.0, "Opening remarks"),
        (330.0, "The core argument"),
        (765.0, "Where it lands"),
    ]


def test_parse_tolerates_preamble_numbering_and_bullets() -> None:
    raw = (
        "Here are the chapters:\n"
        "1. 00:00 Opening\n"
        "- 04:10 Second topic\n"
        "* 08:00 - Third topic\n"
        "Based on the transcript, that's it.\n"
    )
    assert chapters.parse_llm_chapters(raw, duration_secs=1800) == [
        (0.0, "Opening"),
        (250.0, "Second topic"),
        (480.0, "Third topic"),
    ]


def test_parse_accepts_hms_and_long_minutes() -> None:
    raw = "00:00 Start\n1:05:30 Past the hour\n95:00 Long minutes"
    parsed = chapters.parse_llm_chapters(raw, duration_secs=8000)
    assert (3930.0, "Past the hour") in parsed
    assert (5700.0, "Long minutes") in parsed


def test_parse_drops_timestamps_past_the_episode() -> None:
    raw = "00:00 Start\n05:00 Real\n99:00 Beyond the end"
    assert chapters.parse_llm_chapters(raw, duration_secs=600) == [
        (0.0, "Start"),
        (300.0, "Real"),
    ]


def test_parse_forces_a_chapter_at_zero() -> None:
    assert chapters.parse_llm_chapters("05:00 Late start", duration_secs=1800) == [
        (0.0, "Introduction"),
        (300.0, "Late start"),
    ]


def test_parse_keeps_first_title_per_timestamp_and_sorts() -> None:
    raw = "05:00 Second\n00:00 First\n05:00 Duplicate ignored"
    assert chapters.parse_llm_chapters(raw, duration_secs=1800) == [
        (0.0, "First"),
        (300.0, "Second"),
    ]


def test_parse_empty_or_junk_reply_returns_empty() -> None:
    assert chapters.parse_llm_chapters("no chapters here", duration_secs=1800) == []
    assert chapters.parse_llm_chapters("", duration_secs=1800) == []


def test_parse_strips_title_punctuation_and_quotes() -> None:
    raw = '00:00 "Opening remarks."\n02:00 Second topic:'
    assert chapters.parse_llm_chapters(raw, duration_secs=1800) == [
        (0.0, "Opening remarks"),
        (120.0, "Second topic"),
    ]


# --- output document --------------------------------------------------------


def test_build_chapters_json_uses_integer_start_times() -> None:
    doc = json.loads(chapters.build_chapters_json([(0.0, "Opening"), (31.4, "Middle")]))
    assert doc["version"] == "1.2.0"
    assert doc["chapters"] == [
        {"startTime": 0, "title": "Opening"},
        {"startTime": 31, "title": "Middle"},
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


def test_embed_chapters_replaces_existing_frames(env, tmp_path) -> None:
    """A regenerate must not stack chapters on top of the old ones."""

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
    audio.embed_chapters(mp3, [(0.0, "Only one now")], total_duration_secs=2.0)

    tags = ID3(str(mp3))
    assert len(tags.getall("CHAP")) == 1
    assert len(tags.getall("CTOC")) == 1
