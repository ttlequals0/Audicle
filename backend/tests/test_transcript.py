from __future__ import annotations

import re

import pytest
from app.services import transcript


def _chunks(*specs: tuple[str, float]) -> list[transcript.TranscriptChunk]:
    return [transcript.TranscriptChunk(text=t, duration_secs=d) for t, d in specs]


def test_empty_chunks_returns_header_only() -> None:
    vtt = transcript.build_vtt([], silence_ms=250)
    assert vtt == "WEBVTT\n"


def test_single_cue_starts_at_zero() -> None:
    vtt = transcript.build_vtt(
        _chunks(("Hello world.", 5.5)),
        silence_ms=250,
    )
    assert vtt.startswith("WEBVTT\n\n1\n")
    assert "00:00:00.000 --> 00:00:05.500\nHello world.\n" in vtt


def test_multi_cue_inserts_silence_padding() -> None:
    """Build plan example: 12.450 then 12.700 = 250ms gap matching the audio
    pipeline's silence padding."""

    vtt = transcript.build_vtt(
        _chunks(
            (
                "When the cache hit rate dropped to 47 percent, the team investigated.",
                12.45,
            ),
            (
                "They found that the TTL of 300 seconds was too aggressive.",
                12.1,
            ),
        ),
        silence_ms=250,
    )
    assert "00:00:00.000 --> 00:00:12.450" in vtt
    # cue 2 starts at 12.450 + 0.250 = 12.700; ends 12.700 + 12.100 = 24.800
    assert "00:00:12.700 --> 00:00:24.800" in vtt


def test_cues_are_numbered_sequentially() -> None:
    vtt = transcript.build_vtt(
        _chunks(
            ("Cue one.", 1.0),
            ("Cue two.", 1.0),
            ("Cue three.", 1.0),
        ),
        silence_ms=0,
    )
    nums = [int(m) for m in re.findall(r"^(\d+)$", vtt, flags=re.MULTILINE)]
    assert nums == [1, 2, 3]


def test_timestamps_render_hours_correctly() -> None:
    # 3600s + 90s + 0.5s = 3690.5s
    vtt = transcript.build_vtt(
        _chunks(("Skip ahead.", 3690.5)),
        silence_ms=0,
    )
    assert "00:00:00.000 --> 01:01:30.500" in vtt


def test_escapes_special_characters_in_cue_text() -> None:
    vtt = transcript.build_vtt(
        _chunks(("a & b < c > d", 1.0)),
        silence_ms=0,
    )
    assert "a &amp; b &lt; c &gt; d" in vtt
    # ``&`` must be escaped FIRST so the entities aren't double-encoded.
    assert "&amp;amp;" not in vtt


def test_zero_silence_padding_is_allowed() -> None:
    vtt = transcript.build_vtt(
        _chunks(("a", 1.0), ("b", 1.0)),
        silence_ms=0,
    )
    assert "00:00:01.000 --> 00:00:02.000" in vtt


def test_negative_silence_rejected() -> None:
    with pytest.raises(ValueError, match="silence_ms"):
        transcript.build_vtt(_chunks(("a", 1.0)), silence_ms=-1)


def test_negative_duration_rejected() -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        transcript.build_vtt(_chunks(("a", -1.0)), silence_ms=0)


def test_nan_duration_rejected() -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        transcript.build_vtt(_chunks(("a", float("nan"))), silence_ms=0)


def test_inf_duration_rejected() -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        transcript.build_vtt(_chunks(("a", float("inf"))), silence_ms=0)


def test_internal_newlines_flattened_to_spaces() -> None:
    """A blank line terminates a VTT cue, so embedded paragraph breaks must
    be flattened or the next cue's timestamp will be misread as cue text."""

    vtt = transcript.build_vtt(
        _chunks(("first line\n\nsecond line", 1.0)),
        silence_ms=0,
    )
    assert "first line second line" in vtt
    assert "\n\nsecond line" not in vtt


def test_cumulative_drift_stays_integer_ms() -> None:
    """Hundreds of cues at irrational duration must not drift; the cumulative
    timeline is integer ms so the final cue end is exactly
    sum(round(d*1000)) + (n-1)*silence_ms."""

    n = 300
    duration = 12.345
    silence_ms = 250
    chunks = _chunks(*[("cue", duration)] * n)
    vtt = transcript.build_vtt(chunks, silence_ms=silence_ms)
    expected_total_ms = n * round(duration * 1000) + (n - 1) * silence_ms
    h, rem = divmod(expected_total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    expected_end = f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
    assert f"--> {expected_end}\n" in vtt


def test_output_terminates_with_newline() -> None:
    vtt = transcript.build_vtt(_chunks(("a", 1.0)), silence_ms=0)
    assert vtt.endswith("\n") and not vtt.endswith("\n\n")


def test_text_from_vtt_strips_structure_and_unescapes() -> None:
    vtt = (
        "WEBVTT\n\n"
        "1\n00:00:00.000 --> 00:00:02.500\nFirst line &amp; more.\n\n"
        "2\n00:00:02.500 --> 00:00:05.000\nSecond <cue> line.\n"
    )
    assert transcript.text_from_vtt(vtt) == "First line & more.\n\nSecond <cue> line."


def test_text_from_vtt_round_trips_build_vtt() -> None:
    chunks = _chunks(
        ("The CDMA network is up.", 2.0),
        ("Latency dropped by 30 percent.", 2.0),
    )
    vtt = transcript.build_vtt(chunks, silence_ms=200)
    assert transcript.text_from_vtt(vtt) == (
        "The CDMA network is up.\n\nLatency dropped by 30 percent."
    )


def test_text_from_vtt_empty_doc() -> None:
    assert transcript.text_from_vtt("WEBVTT\n") == ""
