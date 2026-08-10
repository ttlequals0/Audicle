"""Chapter generation (3b).

Chapters are decided after the audio stage, when the measured per-chunk
durations are known: one LLM call returns start indices with short titles, and
an index becomes a timestamp from the shared chunk timeline. No ASR.

The document follows the Podcasting 2.0 chapters spec (version 1.2.0). The
audio module embeds the same pairs as ID3 CHAP/CTOC frames, because some
clients (Castro) read only those.
"""

from __future__ import annotations

import json
import re

_LINE_RE = re.compile(r"(\d+)\s*[|:.-]\s*(.+)")


def parse_llm_chapters(raw: str, chunk_count: int) -> list[tuple[int, str]]:
    """Parse ``index | title`` lines from the LLM reply.

    Junk lines and out-of-range indices are dropped, the first title per index
    wins, and output is sorted. A non-empty result always starts at chunk 0
    (titled Introduction if the model gave no chapter there).
    """

    found: dict[int, str] = {}
    for line in raw.splitlines():
        match = _LINE_RE.match(line.strip().lstrip("-* "))
        if not match:
            continue
        index = int(match.group(1))
        title = match.group(2).strip()
        if 0 <= index < chunk_count and title and index not in found:
            found[index] = title
    if not found:
        return []
    found.setdefault(0, "Introduction")
    return sorted(found.items())


def build_chapters_json(timed: list[tuple[float, str]]) -> str:
    """The Podcasting 2.0 chapters document for ``(start_secs, title)`` pairs.

    Timestamps come from ``transcript.chunk_start_ms``, the same timeline the
    VTT cues use."""

    return json.dumps(
        {
            "version": "1.2.0",
            "chapters": [{"startTime": start, "title": title} for start, title in timed],
        },
        ensure_ascii=False,
    )
