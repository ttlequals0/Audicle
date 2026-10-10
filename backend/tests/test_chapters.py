"""Chapter generation (3b): timestamped-transcript prompt input, tolerant
parsing of the LLM's ``MM:SS Title`` reply, and the Podcasting 2.0 document."""

from __future__ import annotations

import json

import pytest
from app.config import get_settings
from app.core import database
from app.services import chapters, llm, pipeline, prompt, transcript


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


def test_timestamped_transcript_preserves_full_cue_text() -> None:
    text = chapters.timestamped_transcript([(0.0, "word " * 200)])
    assert text == "[00:00] " + " ".join(("word " * 200).split())


def test_chapter_geometry_clamps_environment_values(env) -> None:
    settings = get_settings().model_copy(
        update={
            "CHAPTERS_TARGET_SECONDS": 3600,
            "CHAPTERS_WINDOW_SECONDS": 900,
            "CHAPTERS_MIN_CHAPTER_SECONDS": 900,
            "CHAPTERS_MAX_BOUNDARIES": 0,
        }
    )
    assert chapters.resolve_geometry(settings) == (900, 900, 1, 900)
    settings = settings.model_copy(update={"CHAPTERS_TARGET_SECONDS": 120})
    assert chapters.resolve_geometry(settings) == (120, 900, 1, 120)


def test_normalize_merges_short_chapters_and_keeps_opening() -> None:
    assert chapters.normalize_boundaries(
        [
            (0, "Opening"),
            (30, "Duplicate"),
            (300, "Brief"),
            (360, "Main topic"),
            (900, "Closing"),
            (990, "Short tail"),
        ],
        1000,
        180,
    ) == [(0, "Opening"), (360, "Main topic")]


def test_normalize_merges_short_chain_into_previous_topic() -> None:
    assert chapters.normalize_boundaries(
        [(0, "Opening"), (1000, "Topic"), (1100, "Brief change"), (1200, "Final topic")],
        2000,
        180,
    ) == [(0, "Opening"), (1000, "Topic"), (1200, "Final topic")]


def test_normalize_forces_a_chapter_at_zero() -> None:
    assert chapters.normalize_boundaries(
        chapters.parse_llm_chapters("05:00 Late start", duration_secs=1800), 1800, 180
    ) == [
        (0.0, "Introduction"),
        (300.0, "Late start"),
    ]


@pytest.fixture
def chapter_database(env):
    database.run_migrations(env)


async def test_long_episode_uses_windows_and_exceeds_old_chapter_cap(
    env, monkeypatch, chapter_database
) -> None:
    settings = get_settings()
    cues = [
        (float(at), f"Topic at {at}. " + "Full narration. " * 40) for at in range(0, 10800, 600)
    ]
    calls = []
    beats = []
    with database.connection(env) as conn:
        prompt.save_override(conn, "chapters", "Prefer concrete topic titles.", max_bytes=1000)

    async def detect(_system, user, _settings):
        index = len(calls)
        calls.append(user)
        assert "Prefer concrete topic titles." in user
        assert "Full narration. " * 30 in user
        start = index * 2700
        assert f"Window: {start} to {start + 2700} seconds." in user
        assert "up to 4 topic changes" in user
        selected = [(at, text) for at, text in cues if start <= at < start + 2700]
        if index:
            assert f"The current chapter is 'Topic {(2400, 4800, 7200)[index - 1]}'" in user
            assert "[00:00]" not in user
        return "\n".join(f"{int(at) // 60:02d}:00 Topic {int(at)}" for at, _ in selected)

    monkeypatch.setattr(pipeline, "_llm_with_retry", detect)
    monkeypatch.setattr(pipeline, "_beat", lambda: beats.append(True))
    document = await pipeline.generate_chapters_document(cues, 10800, None, settings)
    assert document is not None
    result = json.loads(document)["chapters"]
    assert len(calls) == 4
    assert len(beats) == 4
    assert len(result) > 7
    assert result[0]["startTime"] == 0
    assert result[-1]["startTime"] == 10200
    assert "[140:00]" in calls[-1]


@pytest.mark.parametrize("allow_partial", [False, True])
async def test_failed_window_preserves_strict_run_or_keeps_partial(
    env, monkeypatch, allow_partial, chapter_database
):
    calls = []
    cues = [
        (0, "Opening"),
        (600, "Next topic"),
        (2700, "Middle"),
        (3300, "Middle topic"),
        (5400, "Late topic"),
        (6000, "Last topic"),
    ]

    async def detect(_system, user, _settings):
        calls.append(user)
        if "Window: 2700" in user:
            raise llm.LLMProviderError("provider unavailable")
        if "Window: 0" in user:
            return "00:00 Opening\n10:00 Next topic"
        assert "The current chapter is 'Next topic'" in user
        return "90:00 Late topic\n100:00 Last topic"

    monkeypatch.setattr(pipeline, "_llm_with_retry", detect)
    result = await pipeline.generate_chapters_document(
        cues, 8100, None, get_settings(), allow_partial=allow_partial
    )
    if allow_partial:
        assert len(calls) == 3
        assert [x["startTime"] for x in json.loads(result)["chapters"]] == [0, 600, 5400, 6000]
    else:
        assert result is None
        assert len(calls) == 2


async def test_empty_window_and_short_tail_need_no_fabricated_boundaries(
    env, monkeypatch, chapter_database
):
    calls = []

    async def detect(_system, user, _settings):
        calls.append(user)
        return "00:00 Opening\n10:00 Next topic" if len(calls) == 1 else "NONE"

    monkeypatch.setattr(pipeline, "_llm_with_retry", detect)
    result = await pipeline.generate_chapters_document(
        [(0, "Opening"), (600, "Next topic"), (2700, "Same topic"), (5400, "Tail")],
        5500,
        None,
        get_settings(),
    )
    assert len(calls) == 2
    assert [x["startTime"] for x in json.loads(result)["chapters"]] == [0, 600]


async def test_boundary_cap_stops_calls_and_discards_invented_timestamps(
    env, monkeypatch, chapter_database
):
    calls = []

    async def detect(_system, user, _settings):
        calls.append(user)
        return "00:00 Opening\n05:00 Invented\n10:00 Real\n20:00 Over budget\n99:00 Outside"

    monkeypatch.setattr(pipeline, "_llm_with_retry", detect)
    result = await pipeline.generate_chapters_document(
        [(0, "Opening"), (600, "Real"), (1200, "Over budget"), (3000, "Later")],
        5400,
        None,
        get_settings().model_copy(update={"CHAPTERS_MAX_BOUNDARIES": 1}),
    )
    assert len(calls) == 1
    assert [x["startTime"] for x in json.loads(result)["chapters"]] == [0, 600]


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


def test_parse_window_does_not_force_zero_or_accept_invalid_seconds() -> None:
    assert chapters.parse_llm_chapters("95:00 Real\n95:70 Invalid\n1:70:00 Invalid hour", 8000) == [
        (5700.0, "Real")
    ]


@pytest.mark.parametrize("raw", ["NONE", "none", "None.", "```\nNONE\n```"])
def test_window_accepts_no_change_reply(raw):
    assert chapters.window_boundaries(raw, [(2700, "Continuing topic")], 2700, 5400, 4) == []


def test_window_rejects_reply_with_only_invented_timestamps():
    with pytest.raises(ValueError, match="no valid window timestamps"):
        chapters.window_boundaries("50:01 Invented topic", [(3000.25, "Real cue")], 2700, 5400, 4)


def test_window_accepts_default_silence_marker_without_rounding_up():
    assert chapters.window_boundaries(
        "05:00 Next topic", [(0, "Opening"), (300.25, "Next topic")], 0, 2700, 4
    ) == [(300, "Next topic")]


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


async def test_embed_chapters_writes_chap_and_ctoc_frames(env, tmp_path) -> None:
    import numpy as np
    import soundfile as sf
    from app.config import get_settings
    from app.services import audio
    from mutagen.id3 import ID3

    src_wav = tmp_path / "in.wav"
    t = np.arange(24000 * 2) / 24000
    sf.write(str(src_wav), (0.3 * np.sin(2 * np.pi * 220 * t)).astype("float32"), 24000)
    mp3 = tmp_path / "out.mp3"
    await audio.normalize_and_encode(src_wav, mp3, get_settings())

    audio.embed_chapters(mp3, [(0.0, "One"), (1.0, "Two")], total_duration_secs=2.0)

    tags = ID3(str(mp3))
    chaps = tags.getall("CHAP")
    assert len(chaps) == 2
    assert chaps[0].start_time == 0 and chaps[0].end_time == 1000
    assert chaps[1].start_time == 1000 and chaps[1].end_time == 2000
    ctoc = tags.getall("CTOC")
    assert len(ctoc) == 1
    assert list(ctoc[0].child_element_ids) == [c.element_id for c in chaps]


async def test_embed_chapters_replaces_existing_frames(env, tmp_path) -> None:
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
    await audio.normalize_and_encode(src_wav, mp3, get_settings())

    audio.embed_chapters(mp3, [(0.0, "One"), (1.0, "Two")], total_duration_secs=2.0)
    audio.embed_chapters(mp3, [(0.0, "Only one now")], total_duration_secs=2.0)

    tags = ID3(str(mp3))
    assert len(tags.getall("CHAP")) == 1
    assert len(tags.getall("CTOC")) == 1
