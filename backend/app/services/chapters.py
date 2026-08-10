"""Chapter generation (3b).

The LLM sees the narration as a timestamped transcript (``[MM:SS] text``) and
answers with ``MM:SS Title`` lines, the shape MinusPod runs in production.
Timestamps come from the transcript itself, so a chapter start needs no index
bookkeeping and a reply that drifts from the format still parses.

The document follows the Podcasting 2.0 chapters spec (version 1.2.0). The
audio module embeds the same pairs as ID3 CHAP/CTOC frames, because some
clients (Castro) read only those.
"""

from __future__ import annotations

import json
import re

# ``MM:SS`` or ``H:MM:SS``, minutes allowed past 59 so a long episode's later
# lines still parse. An optional ``-``/``:`` may separate the title.
_LINE_RE = re.compile(r"^(\d{1,3}):(\d{2})(?::(\d{2}))?\s*[-:]?\s*(.+)$")
# Conversational preamble the model sometimes prepends despite the prompt.
_PREAMBLE_RE = re.compile(r"^(here|based|these|the |i )", re.IGNORECASE)
# One transcript line per chunk, trimmed to keep a long prompt manageable.
_LINE_CHARS = 220


def timestamped_transcript(cues: list[tuple[float, str]]) -> str:
    """Render ``(start_secs, text)`` cues as ``[MM:SS] text`` lines."""

    lines = []
    for start, text in cues:
        minutes, seconds = divmod(int(start), 60)
        flat = " ".join(text.split())[:_LINE_CHARS]
        lines.append(f"[{minutes:02d}:{seconds:02d}] {flat}")
    return "\n".join(lines)


def parse_llm_chapters(raw: str, duration_secs: float) -> list[tuple[float, str]]:
    """Parse ``MM:SS Title`` lines into sorted ``(start_secs, title)`` pairs.

    Tolerant by design: preamble, list numbering, and bullets are stripped
    before matching and unparseable lines are skipped. Timestamps outside the
    episode are dropped, the first title per timestamp wins, and a non-empty
    result always starts at 0.
    """

    found: dict[float, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or _PREAMBLE_RE.match(stripped):
            continue
        stripped = re.sub(r"^\d+[.)]\s*", "", stripped)
        stripped = re.sub(r"^[-*]+\s*", "", stripped)
        match = _LINE_RE.match(stripped)
        if not match:
            continue
        first, second, third, title = match.groups()
        if third is not None:  # H:MM:SS
            start = int(first) * 3600 + int(second) * 60 + int(third)
        else:  # MM:SS, minutes may exceed 59
            start = int(first) * 60 + int(second)
        title = title.strip().strip("\"'").rstrip(".:;,-").strip()
        if title and 0 <= start < duration_secs and float(start) not in found:
            found[float(start)] = title
    if not found:
        return []
    found.setdefault(0.0, "Introduction")
    return sorted(found.items())


def build_chapters_json(timed: list[tuple[float, str]]) -> str:
    """The Podcasting 2.0 chapters document for ``(start_secs, title)`` pairs.

    ``startTime`` is whole seconds, which is what clients display and what
    MinusPod ships."""

    return json.dumps(
        {
            "version": "1.2.0",
            "chapters": [
                {"startTime": round(start), "title": title} for start, title in timed
            ],
        },
        ensure_ascii=False,
    )
