"""WebVTT transcript generation.

Inputs are chunks of cleaned narration text paired with the per-chunk
duration the TTS wrapper reported, plus the silence padding the audio
pipeline inserts between chunks. Output is a single VTT string suitable
for the ``transcript_vtt`` column on episodes.

The cumulative timeline mirrors the produced audio:

    start[0]   = 0
    end[0]     = duration[0]
    start[i+1] = end[i] + silence_ms / 1000
    end[i+1]   = start[i+1] + duration[i+1]

The cursor is tracked as integer milliseconds rather than float seconds so
long episodes (hundreds of cues) don't accumulate float drift relative to
the produced MP3.

Special characters in cue text (``<``, ``>``, ``&``) are escaped per the
VTT spec so the file remains valid HTML-ish text; embedded newlines are
flattened to spaces because a blank line terminates a VTT cue.
"""

from __future__ import annotations

import html
import logging
import math
from dataclasses import dataclass

logger = logging.getLogger("app.services.transcript")


@dataclass(frozen=True)
class TranscriptChunk:
    text: str
    duration_secs: float


def build_vtt(chunks: list[TranscriptChunk], silence_ms: int) -> str:
    """Render a WebVTT document from chunk text + per-chunk durations.

    ``silence_ms`` is the silence padding the audio pipeline inserted between
    chunks (``TTS_CHUNK_SILENCE_MS``); it offsets each cue's start by that
    much from the prior cue's end so VTT timestamps align with the produced
    MP3.

    Empty chunk list returns the WEBVTT header only; consumers can detect
    that by checking ``len(lines) == 1``.
    """

    if silence_ms < 0:
        raise ValueError(f"silence_ms must be >= 0, got {silence_ms}")

    lines: list[str] = ["WEBVTT", ""]
    cursor_ms = 0
    for index, chunk in enumerate(chunks):
        if not math.isfinite(chunk.duration_secs) or chunk.duration_secs < 0:
            raise ValueError(f"chunk {index} has invalid duration {chunk.duration_secs!r}")
        duration_ms = round(chunk.duration_secs * 1000)
        start_ms = cursor_ms
        end_ms = cursor_ms + duration_ms
        lines.append(str(index + 1))
        lines.append(f"{_format_ts(start_ms)} --> {_format_ts(end_ms)}")
        lines.append(_escape_cue(chunk.text))
        lines.append("")
        # Silence padding is BETWEEN cues, not after the last one; mirrors
        # the audio pipeline's concat_with_padding behavior so VTT and MP3
        # total durations match.
        cursor_ms = end_ms + silence_ms if index < len(chunks) - 1 else end_ms

    # Drop the trailing blank line so the file ends with exactly one \n via
    # the join below.
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def text_from_vtt(vtt: str) -> str:
    """Reconstruct plain narration text from a WebVTT transcript.

    Used to backfill ``cleaned_text`` for episodes processed before 0.6.0, when
    the column did not exist. Drops the WEBVTT header, NOTE blocks, cue-number
    lines, and timestamp lines; un-escapes the cue payloads; joins cues with a
    blank line. Paragraph structure is approximate -- cues mirror TTS chunks,
    not the source paragraphs -- but the words match what the audio says.
    """

    cues: list[str] = []
    current: list[str] = []
    in_cue = False
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line:
            if current:
                cues.append(" ".join(current))
                current = []
            in_cue = False
            continue
        if "-->" in line:
            in_cue = True
            continue
        if not in_cue:
            # WEBVTT header, NOTE blocks, or a standalone cue-number line.
            continue
        current.append(html.unescape(line))
    if current:
        cues.append(" ".join(current))
    return "\n\n".join(cues)


def _format_ts(total_ms: int) -> str:
    """Render ``total_ms`` as ``HH:MM:SS.mmm``."""

    if total_ms < 0:
        total_ms = 0
    hours, remainder_ms = divmod(total_ms, 3_600_000)
    minutes, remainder_ms = divmod(remainder_ms, 60_000)
    secs, millis = divmod(remainder_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _escape_cue(text: str) -> str:
    """Escape ``&``, ``<``, ``>`` per VTT cue payload rules, and flatten
    embedded newlines: a blank line ends a cue, so a chunk with an internal
    paragraph break would otherwise split the cue and desync timestamps."""

    flattened = text.replace("\r\n", "\n").replace("\r", "\n")
    # ``str.split()`` with no arg collapses any run of whitespace (newlines,
    # tabs, spaces) so paragraph breaks don't leave double spaces in the cue.
    flattened = " ".join(flattened.split())
    return html.escape(flattened, quote=False)
