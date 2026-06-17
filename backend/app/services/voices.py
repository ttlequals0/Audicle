"""Reference-voice slots (0.31.0; slots-only since 0.35.0).

Five fixed slots back the multi-voice feature. A slot is "filled" when its WAV
exists on disk at ``reference/voices/slot{n}.wav`` (mounted into the TTS wrapper).
Slot labels are editable, stored as one JSON blob in the ``settings`` table. A
job's voice is resolved once at submit time to a slot id (recorded on
``jobs.voice_id``); submit/upload reject when no slot is filled, so a live job
always has at least one voice to pick from. A ``None`` ``voice_id`` only appears on
pre-slots rows (or a job submitted before any slot existed) and falls back to
:func:`default_slot` at synthesis time.
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


def default_slot() -> int | None:
    """The wrapper's resting voice -- the lowest-numbered filled slot, or ``None``
    when no slot is filled. Mirrors the wrapper's own boot pick so a job with no
    recorded voice lands on the same slot the wrapper booted on."""

    filled = filled_slots()
    return filled[0] if filled else None


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
    id as a string, or ``None`` when no slots are filled (submit/upload guard against
    that, so callers normally never see it). An empty/forced slot or a stale "last"
    falls back to a random filled slot.
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


def label_for(conn: sqlite3.Connection, voice_id: str | None) -> str:
    """Human-readable name for the voice a job used, snapshotted onto the episode
    at finalize. A recorded slot returns its label (or ``Slot N`` when unlabelled).
    A ``None`` voice_id (a job that fell back to the default) resolves to the
    default slot's label -- the voice synthesis actually used -- so the recorded
    label matches the audio; ``Default`` only when no slot exists at all."""

    if not voice_id:
        slot = default_slot()
        if slot is None:
            return "Default"
        voice_id = str(slot)
    return get_labels(conn).get(str(voice_id)) or f"Slot {voice_id}"


def _last_voice_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT voice_id FROM jobs WHERE voice_id IS NOT NULL ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return row["voice_id"] if row else None
