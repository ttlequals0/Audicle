"""Chapter generation (3b).

Chapters are decided after the audio stage, when the pipeline holds the chunk
list, the measured per-chunk durations, and the real MP3 duration: one LLM call
over the numbered chunk list returns start indices with short titles, and a
chunk index converts to a timestamp by summing the preceding durations plus the
inter-chunk silence. Deterministic, no ASR.

The JSON document follows the Podcasting 2.0 chapters spec
(application/json+chapters, version 1.2.0). The same (start, title) pairs are
embedded into the MP3 as ID3 CHAP/CTOC frames by the audio module, because
some clients (Castro) read only the embedded frames.
"""

from __future__ import annotations

import json
import re

_LINE_RE = re.compile(r"(\d+)\s*[|:.-]\s*(.+)")


def parse_llm_chapters(raw: str, chunk_count: int) -> list[tuple[int, str]]:
    """Parse ``index | title`` lines from the LLM reply.

    Junk lines are ignored, indices outside the chunk range are dropped, the
    first title per index wins, and output is sorted. A non-empty result always
    includes a chapter at chunk 0 (Podcasting 2.0 wants the first chapter at
    the start), titled Introduction when the model did not provide one.
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
