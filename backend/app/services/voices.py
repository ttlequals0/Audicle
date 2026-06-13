"""Reference-voice slots (0.31.0).

Five fixed slots back the multi-voice feature. A slot is "filled" when its WAV
exists on disk at ``reference/voices/slot{n}.wav`` (mounted read-only into the TTS
wrapper next to the legacy ``voice.wav``). Slot labels are editable, stored as one
JSON blob in the ``settings`` table. A job's voice is resolved once at submit time
to a slot id (recorded on ``jobs.voice_id``); ``None`` means the legacy single
``voice.wav`` (no slots filled).
"""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

from app.services import settings_store

NUM_SLOTS = 5
_LABELS_KEY = "reference_slot_labels"


def voices_dir() -> Path:
    """``backend/app/reference/voices`` -- next to the legacy ``voice.wav``."""

    return Path(__file__).resolve().parent.parent / "reference" / "voices"


def slot_path(slot: int) -> Path:
    return voices_dir() / f"slot{slot}.wav"


def filled_slots() -> list[int]:
    return [n for n in range(1, NUM_SLOTS + 1) if slot_path(n).is_file()]


def get_labels(conn: sqlite3.Connection) -> dict[str, str]:
    raw = settings_store.get(conn, _LABELS_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def set_label(conn: sqlite3.Connection, slot: int, label: str) -> None:
    """Set (or clear, when blank) a slot's display label."""

    labels = get_labels(conn)
    label = label.strip()
    if label:
        labels[str(slot)] = label[:60]
    else:
        labels.pop(str(slot), None)
    settings_store.set_(conn, _LABELS_KEY, json.dumps(labels))


def resolve(conn: sqlite3.Connection, choice: str | None) -> str | None:
    """Resolve a hero-box voice choice to a slot id, recorded on the job.

    ``choice`` is a slot number ("1".."5"), "last", "random"/None. Returns the slot
    id as a string, or ``None`` when no slots are filled (legacy voice.wav). An
    empty/forced slot or a stale "last" falls back to a random filled slot.
    """

    filled = filled_slots()
    if not filled:
        return None
    if choice and choice.isdigit() and int(choice) in filled:
        return choice
    if choice == "last":
        last = _last_voice_id(conn)
        if last and last.isdigit() and int(last) in filled:
            return last
    return str(random.choice(filled))


def _last_voice_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT voice_id FROM jobs WHERE voice_id IS NOT NULL ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return row["voice_id"] if row else None
