"""``/api/v1/reference/*`` -- operator-side reference voice management.

Slots-only since 0.35.0: there is no separate committed ``voice.wav``. Voices live
entirely in five fixed slots under ``reference/voices/slot{n}.wav``:

- ``GET    /api/v1/reference/slots`` lists the five slots (filled, label, duration).
- ``POST   /api/v1/reference/slots/{n}`` installs/replaces a slot's clip.
- ``DELETE /api/v1/reference/slots/{n}`` clears a slot (refused for the last one,
  so at least one voice is always loaded).
- ``PUT    /api/v1/reference/slots/{n}/label`` renames a slot.
- ``GET    /api/v1/reference/slots/{n}/preview`` returns the stored clip.
- ``POST   /api/v1/reference/slots/{n}/audition`` synthesizes a sample with the slot
  on the wrapper, then restores the wrapper's resting voice.
"""

from __future__ import annotations

import asyncio
import io
import logging
import sqlite3
import wave
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app.api.deps import get_conn
from app.config import Settings, get_settings
from app.core import database
from app.services import voices
from app.services.atomic_write import write_bytes_atomic

logger = logging.getLogger("app.api.reference")
router = APIRouter(prefix="/reference", tags=["reference"])

# Voice clip spec: mono, 24 kHz target (16-48 acceptable),
# 6-15 s, ~150 kB-1.5 MB. The caps below are deliberate floors/ceilings
# that catch obvious mis-uploads without rejecting borderline-good clips.
_MAX_REFERENCE_BYTES = 5 * 1024 * 1024
_MIN_DURATION_SECS = 3.0
_MAX_DURATION_SECS = 60.0

DEFAULT_SAMPLE_TEXT = (
    "But I must explain to you how all this mistaken idea of denouncing "
    "of a pleasure and praising pain was born and I will give you a "
    "complete account of the system, and expound the actual teachings of "
    "the great explorer of the truth, the master-builder of human happiness."
)

# Serialises the slot-audition critical section. The in-process asyncio.Lock gives
# intra-worker fairness and a cheap fast path; the cross-process fcntl flock
# (database.reference_lock) is what prevents two auditions (each select-voice +
# generate, then restore the resting voice) from interleaving across uvicorn
# --workers N and leaving the wrapper switched to the wrong slot.
_reference_lock = asyncio.Lock()


@asynccontextmanager
async def _serialized_reference_access(data_dir: Path) -> AsyncIterator[None]:
    """Hold the in-process lock and the cross-process flock for the duration of
    a slot-audition critical section. The asyncio.Lock queues same-process
    callers cheaply; the flock (cancellation-safe, see reference_lock_async)
    serializes across worker processes."""

    async with _reference_lock, database.reference_lock_async(data_dir):
        yield


def _validate_wav(data: bytes) -> tuple[int, float]:
    """Return ``(sample_rate, duration_secs)`` if ``data`` is a readable
    WAV; raise HTTPException(400) otherwise."""

    if len(data) > _MAX_REFERENCE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"voice clip must be <= {_MAX_REFERENCE_BYTES} bytes",
        )
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            sample_rate = wav.getframerate()
            frames = wav.getnframes()
    except (wave.Error, EOFError) as exc:
        raise HTTPException(
            status_code=400, detail=f"could not read WAV: {exc}"
        ) from exc
    duration = frames / sample_rate if sample_rate else 0.0
    if duration < _MIN_DURATION_SECS or duration > _MAX_DURATION_SECS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"voice clip duration {duration:.1f}s outside "
                f"[{_MIN_DURATION_SECS}, {_MAX_DURATION_SECS}] window"
            ),
        )
    return sample_rate, duration


def _coerce_to_wav(data: bytes) -> bytes:
    """Accept any common audio upload: a valid WAV passes through untouched,
    anything else (mp3, m4a/aac, flac, ogg/opus, ...) is transcoded to a mono
    WAV via ffmpeg. Raises HTTPException(400) when the bytes aren't decodable."""

    try:
        with wave.open(io.BytesIO(data), "rb"):
            return data
    except Exception:
        # Any wave parse failure (wave.Error/EOFError/RuntimeError) means "not a
        # clean WAV" -- fall through and let ffmpeg try to decode it.
        pass
    # Lazy import: audio pulls in torch, which we don't want loaded into the API
    # process at boot just for this operator-only transcode path.
    from app.services import audio

    try:
        return audio.transcode_to_wav(data)
    except audio.FfmpegError as exc:
        raise HTTPException(
            status_code=400, detail="unsupported or unreadable audio file"
        ) from exc


def _prepare_reference_wav(data: bytes) -> tuple[bytes, int, float]:
    """Single upload gate: coerce any supported format to WAV (mp3/m4a/flac/ogg
    via ffmpeg; a real WAV passes through) then validate duration and size.
    Returns ``(wav_bytes, sample_rate, duration_secs)``. Runs ffmpeg, so callers
    invoke it off the event loop via ``asyncio.to_thread``."""

    wav = _coerce_to_wav(data)
    sample_rate, duration_secs = _validate_wav(wav)
    return wav, sample_rate, duration_secs


async def _read_upload_capped(voice: UploadFile) -> bytes:
    """Stream the upload into a bytes object, aborting with 400 as soon
    as it crosses the cap so a hostile payload can't fully buffer first."""

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await voice.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_REFERENCE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"voice clip must be <= {_MAX_REFERENCE_BYTES} bytes",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _read_generated_wav(response: httpx.Response, settings: Settings) -> bytes:
    """Validate the wrapper ``/generate`` response and read the produced WAV.

    The wrapper-supplied path is constrained to ``DATA_DIR`` so a compromised or
    misconfigured wrapper can't trick the backend into reading arbitrary files.
    """

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"tts wrapper /generate returned {response.status_code}",
        )
    wav_path = Path(response.json()["wav_path"]).resolve()
    data_root = Path(settings.DATA_DIR).resolve()
    if not wav_path.is_relative_to(data_root) or not wav_path.is_file():
        raise HTTPException(status_code=502, detail="tts wrapper returned an invalid wav path")
    return wav_path.read_bytes()


async def _reload_silently(client: httpx.AsyncClient, settings: Settings) -> None:
    """Best-effort /reload call. Logs HTTP errors so an operator can see
    that the wrapper may be out of sync with disk."""

    try:
        await client.post(f"{settings.TTS_URL.rstrip('/')}/reload")
    except httpx.HTTPError as exc:
        logger.warning(
            "TTS /reload after restore failed",
            extra={"event": "tts_reload_failed", "error": str(exc)},
        )


# --- Voice slots (0.31.0) -- 5 fixed slots picked at random per episode ---------


class SlotInfo(BaseModel):
    slot: int
    filled: bool
    label: str | None = None
    duration_secs: int | None = None


def _wav_seconds(path: Path) -> int | None:
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            frames = w.getnframes()
    except (wave.Error, EOFError, OSError):
        return None
    return round(frames / rate) if rate else None


# A fixed allowlist of slot filenames. Selecting from it (rather than building
# the name from the request value) is the barrier CodeQL recognizes for
# py/path-injection: a bounded slot index can only ever pick a constant
# ``slot{n}.wav`` literal, so the path has no user-controlled component. Must
# stay in sync with ``voices.NUM_SLOTS`` and ``voices.slot_path``'s naming.
_SLOT_FILENAMES = ("slot1.wav", "slot2.wav", "slot3.wav", "slot4.wav", "slot5.wav")


def _safe_slot_path(slot: int) -> Path:
    """Path to slot ``slot``'s WAV, built from the constant allowlist above so no
    user-provided value reaches the filesystem. ``slot`` is already PathParam-bounded
    (1..NUM_SLOTS); the explicit re-check keeps this safe for any internal caller."""

    if not 1 <= slot <= len(_SLOT_FILENAMES):
        raise HTTPException(status_code=404, detail=f"voice slot {slot} not found")
    return voices.voices_dir() / _SLOT_FILENAMES[slot - 1]


@router.get("/slots", response_model=list[SlotInfo])
async def list_slots(conn: Annotated[sqlite3.Connection, Depends(get_conn)]) -> list[SlotInfo]:
    labels = voices.get_labels(conn)
    out: list[SlotInfo] = []
    for n in range(1, voices.NUM_SLOTS + 1):
        path = voices.slot_path(n)
        filled = path.is_file()
        out.append(
            SlotInfo(
                slot=n,
                filled=filled,
                label=labels.get(str(n)),
                duration_secs=_wav_seconds(path) if filled else None,
            )
        )
    return out


@router.post("/slots/{slot}")
async def upload_slot(
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    slot: Annotated[int, PathParam(ge=1, le=voices.NUM_SLOTS)],
    voice: Annotated[UploadFile, File(description="voice clip for this slot (WAV/MP3/M4A/FLAC/OGG)")],
    label: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Install or replace the clip in slot ``slot``. No /reload -- slots are
    selected per job via /select-voice."""

    candidate = await _read_upload_capped(voice)
    candidate, sample_rate, duration_secs = await asyncio.to_thread(
        _prepare_reference_wav, candidate
    )
    write_bytes_atomic(_safe_slot_path(slot), candidate, prefix=".slot-")
    if label is not None:
        voices.set_label(conn, slot, label)
    return {
        "slot": slot,
        "filled": True,
        "sample_rate": sample_rate,
        "duration_secs": round(duration_secs),
    }


@router.delete("/slots/{slot}")
async def clear_slot(
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    slot: Annotated[int, PathParam(ge=1, le=voices.NUM_SLOTS)],
) -> dict[str, Any]:
    # At least one voice must stay loaded at all times (a job submitted with no
    # voice has nothing to fall back to). Refuse to empty the only filled slot.
    if voices.filled_slots() == [slot]:
        raise HTTPException(
            status_code=409,
            detail="cannot clear the only loaded voice; upload another slot first",
        )
    with suppress(FileNotFoundError):
        _safe_slot_path(slot).unlink()
    voices.set_label(conn, slot, "")
    return {"slot": slot, "filled": False}


@router.put("/slots/{slot}/label")
async def rename_slot(
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    slot: Annotated[int, PathParam(ge=1, le=voices.NUM_SLOTS)],
    label: Annotated[str, Form(max_length=60)],
) -> dict[str, Any]:
    voices.set_label(conn, slot, label)
    return {"slot": slot, "label": label.strip() or None}


@router.get("/slots/{slot}/preview")
async def preview_slot(
    slot: Annotated[int, PathParam(ge=1, le=voices.NUM_SLOTS)],
) -> FileResponse:
    path = _safe_slot_path(slot)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"voice slot {slot} is empty")
    return FileResponse(path, media_type="audio/wav")


@router.post("/slots/{slot}/audition")
async def audition_slot(
    settings: Annotated[Settings, Depends(get_settings)],
    slot: Annotated[int, PathParam(ge=1, le=voices.NUM_SLOTS)],
    sample_text: Annotated[str, Form(min_length=4, max_length=400)] = DEFAULT_SAMPLE_TEXT,
) -> Response:
    """Synthesize a sample with slot ``slot``: selects it on the wrapper, generates,
    then reloads the wrapper's resting voice so an audition never leaves the wrapper
    switched to the auditioned slot."""

    if not _safe_slot_path(slot).is_file():
        raise HTTPException(status_code=404, detail=f"voice slot {slot} is empty")
    base = settings.TTS_URL.rstrip("/")
    async with _serialized_reference_access(settings.DATA_DIR), httpx.AsyncClient(
        timeout=settings.TTS_HTTP_TIMEOUT_SECONDS
    ) as client:
        try:
            await client.post(f"{base}/select-voice", json={"slot": slot})
            response = await client.post(
                f"{base}/generate",
                json={"text": sample_text, "episode_id": "slotaudition", "chunk_index": 0},
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"tts wrapper call failed: {exc}") from exc
        finally:
            # Reload the wrapper's resting voice (its lowest filled slot) so the
            # auditioned slot never leaks into a later job's synthesis.
            await _reload_silently(client, settings)
        body = _read_generated_wav(response, settings)
    return Response(content=body, media_type="audio/wav")
