"""Chapter generation (3b).

The LLM sees narration windows as a timestamped transcript (``[MM:SS] text``) and
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

from app.config import RUNTIME_SETTING_BOUNDS, Settings, clamp_to_bounds
from app.services import cleanup_output

# ``MM:SS`` or ``H:MM:SS``, minutes allowed past 59 so a long episode's later
# lines still parse. An optional ``-``/``:`` may separate the title.
_LINE_RE = re.compile(r"^(\d{1,3}):([0-5]\d)(?::([0-5]\d))?\s*[-:]?\s*(.+)$")
_MIN_BOUNDARY_SPACING_SECONDS = 60
# Conversational preamble the model sometimes prepends despite the prompt.
_PREAMBLE_RE = re.compile(r"^(here|based|these|the |i )", re.IGNORECASE)


def resolve_geometry(settings: Settings) -> tuple[int, int, int, int]:
    """Clamp environment geometry without blocking startup."""
    keys = (
        "CHAPTERS_TARGET_SECONDS",
        "CHAPTERS_WINDOW_SECONDS",
        "CHAPTERS_MAX_BOUNDARIES",
        "CHAPTERS_MIN_CHAPTER_SECONDS",
    )
    target, window, maximum, minimum = (
        int(clamp_to_bounds(getattr(settings, key), RUNTIME_SETTING_BOUNDS[key])) for key in keys
    )
    target = min(target, window)
    minimum = min(minimum, target)
    return target, window, maximum, minimum


def validate_geometry(settings: Settings) -> None:
    """Reject inconsistent geometry before storing runtime overrides."""
    for smaller, larger in (
        ("CHAPTERS_TARGET_SECONDS", "CHAPTERS_WINDOW_SECONDS"),
        ("CHAPTERS_MIN_CHAPTER_SECONDS", "CHAPTERS_TARGET_SECONDS"),
    ):
        if getattr(settings, smaller) > getattr(settings, larger):
            raise ValueError(f"{smaller} must not exceed {larger}")


def window_context(start: int, end: float, budget: int, previous_title: str | None) -> str:
    """Supply the window budget and continuation rules for any prompt."""
    context = (
        "These window instructions take precedence over earlier count and opening rules. "
        f"Window: {start} to {end:g} seconds. "
        f"Identify up to {budget} topic changes, excluding the opening chapter."
    )
    if start == 0:
        return context + " Include an opening chapter at 00:00."
    context += (
        " Do not add an opening chapter or mark a change just because this window begins."
        " Return NONE if this window has no topic changes."
    )
    if previous_title:
        context += (
            f" The current chapter is {previous_title!r}. Only mark a change away from that topic."
        )
    return context


def window_boundaries(
    raw: str, cues: list[tuple[float, str]], start: int, end: float, budget: int
) -> list[tuple[float, str]]:
    """Keep supplied markers; accept NONE and reject malformed replies."""
    markers = {float(int(at)) for at, _ in cues}
    parsed = [
        pair
        for pair in parse_llm_chapters(raw, end)
        if start <= pair[0] < end and pair[0] in markers
    ]
    if not parsed and raw.strip().strip("` \r\n").rstrip(".!").casefold() not in {"", "none"}:
        raise ValueError("chapter reply has no valid window timestamps")
    return [pair for pair in parsed if pair[0] == 0] + [pair for pair in parsed if pair[0] > 0][
        :budget
    ]


def timestamped_transcript(cues: list[tuple[float, str]]) -> str:
    """Render ``(start_secs, text)`` cues as ``[MM:SS] text`` lines."""

    lines = []
    for start, text in cues:
        minutes, seconds = divmod(int(start), 60)
        flat = " ".join(text.split())
        lines.append(f"[{minutes:02d}:{seconds:02d}] {flat}")
    return "\n".join(lines)


def parse_llm_chapters(raw: str, duration_secs: float) -> list[tuple[float, str]]:
    """Parse ``MM:SS Title`` lines into sorted ``(start_secs, title)`` pairs.

    Tolerant by design: preamble, list numbering, and bullets are stripped
    before matching and unparseable lines are skipped. Timestamps outside the
    episode are dropped and the first title per timestamp wins.
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
        title = cleanup_output.ascii_punctuation(title)
        title = title.strip().strip("\"'").rstrip(".:;,-").strip()
        if title and 0 <= start < duration_secs and float(start) not in found:
            found[float(start)] = title
    if not found:
        return []
    return sorted(found.items())


def normalize_boundaries(
    timed: list[tuple[float, str]], duration_secs: float, minimum_secs: int
) -> list[tuple[float, str]]:
    """Deduplicate nearby boundaries and merge short chapters, retaining zero."""
    if not timed:
        return []
    found = dict(reversed(timed))  # Keep the first title for duplicate timestamps.
    found.setdefault(0.0, "Introduction")
    spaced = []
    for start, title in sorted(found.items()):
        if not spaced or start - spaced[-1][0] >= _MIN_BOUNDARY_SPACING_SECONDS:
            spaced.append((start, title))
    merged = []
    next_start = duration_secs
    for pair in reversed(spaced):
        if pair[0] == 0 or next_start - pair[0] >= minimum_secs:
            merged.append(pair)
            next_start = pair[0]
    return list(reversed(merged))


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
