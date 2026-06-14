"""``/api/v1/reference/*`` -- operator-side reference voice management.

Three endpoints:

- ``GET /api/v1/reference/preview`` returns the currently-installed
  ``voice.wav`` (the clip the TTS wrapper conditions on).
- ``POST /api/v1/reference/test`` accepts a candidate WAV + sample text,
  calls the TTS wrapper using the candidate (without permanently
  committing it), and returns the generated audio for audition.
- ``POST /api/v1/reference/commit`` accepts a candidate WAV, validates,
  atomically swaps it into ``backend/app/reference/voice.wav``, and
  asks the TTS wrapper to ``/reload`` so the next ``/generate`` call
  uses the new voice without a container restart.
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

# Serialises the reference-voice critical section. The in-process asyncio.Lock
# gives intra-worker fairness and a cheap fast path; the cross-process fcntl
# flock (database.reference_lock) is what actually prevents the read-stage-
# generate-restore sequence from interleaving across uvicorn --workers N and
# clobbering the committed voice.wav. /commit takes it too so /test never sees a
# half-swapped voice.
_reference_lock = asyncio.Lock()


@asynccontextmanager
async def _serialized_reference_access(data_dir: Path) -> AsyncIterator[None]:
    """Hold the in-process lock and the cross-process flock for the duration of
    a reference-voice critical section. The asyncio.Lock queues same-process
    callers cheaply; the flock (cancellation-safe, see reference_lock_async)
    serializes across worker processes."""

    async with _reference_lock, database.reference_lock_async(data_dir):
        yield


def _reference_path() -> Path:
    """Where the wrapper picks the voice clip up. Matches the bind mount
    in docker-compose."""

    return Path(__file__).resolve().parent.parent.parent / "reference" / "voice.wav"


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


@router.get("/preview")
async def preview() -> FileResponse:
    path = _reference_path()
    if not path.is_file():
        raise HTTPException(
            status_code=404, detail="reference voice.wav not installed"
        )
    return FileResponse(path, media_type="audio/wav")


@router.get("/status")
async def default_voice_status() -> dict[str, Any]:
    """Whether the fallback voice.wav is installed, plus its duration. The merged
    voices UI renders this as the 'Default' row alongside the slots."""

    path = _reference_path()
    filled = path.is_file()
    return {"filled": filled, "duration_secs": _wav_seconds(path) if filled else None}


@router.post("/test")
async def test_candidate(
    settings: Annotated[Settings, Depends(get_settings)],
    voice: Annotated[UploadFile, File(description="candidate voice clip (WAV/MP3/M4A/FLAC/OGG)")],
    sample_text: Annotated[
        str,
        Form(
            min_length=4,
            max_length=400,
            description="text to synthesize using the candidate clip",
        ),
    ] = DEFAULT_SAMPLE_TEXT,
) -> Response:
    """Synthesize ``sample_text`` using ``voice`` without committing.

    Staged through the live reference path so the wrapper picks it up
    via /reload; on every exit path the committed clip is restored. If
    no clip was previously committed, the candidate is removed on exit
    so /test never silently turns into /commit.
    """

    candidate = await _read_upload_capped(voice)
    candidate, _, _ = await asyncio.to_thread(_prepare_reference_wav, candidate)

    reference = _reference_path()
    reference.parent.mkdir(parents=True, exist_ok=True)

    async with _serialized_reference_access(settings.DATA_DIR):
        backup = reference.read_bytes() if reference.is_file() else None
        try:
            write_bytes_atomic(reference, candidate, prefix=".ref-test-")
            async with httpx.AsyncClient(
                timeout=settings.TTS_HTTP_TIMEOUT_SECONDS
            ) as client:
                try:
                    await client.post(f"{settings.TTS_URL.rstrip('/')}/reload")
                except httpx.HTTPError as exc:
                    raise HTTPException(
                        status_code=502,
                        detail=f"tts wrapper /reload failed: {exc}",
                    ) from exc
                response = await client.post(
                    f"{settings.TTS_URL.rstrip('/')}/generate",
                    json={
                        "text": sample_text,
                        "episode_id": "reftest",
                        "chunk_index": 0,
                    },
                )
                body = _read_generated_wav(response, settings)
                # Restore (or remove if there was nothing committed) and
                # reload BEFORE releasing the lock so the next /generate
                # never sees the candidate.
                _restore_or_clear(reference, backup)
                await _reload_silently(client, settings)
                return Response(content=body, media_type="audio/wav")
        except Exception:
            # Generate path raised after the candidate was staged: roll
            # back disk state on the way out. Reload is best-effort.
            _restore_or_clear(reference, backup)
            async with httpx.AsyncClient(
                timeout=settings.TTS_HTTP_TIMEOUT_SECONDS
            ) as cleanup_client:
                await _reload_silently(cleanup_client, settings)
            raise


@router.post("/audition")
async def audition_committed(
    settings: Annotated[Settings, Depends(get_settings)],
    sample_text: Annotated[
        str,
        Form(
            min_length=4,
            max_length=400,
            description="text to synthesize using the committed voice",
        ),
    ] = DEFAULT_SAMPLE_TEXT,
) -> Response:
    """Synthesize ``sample_text`` with the currently-committed reference voice.

    Unlike ``/test`` there is no upload and no staging -- it just exercises the
    wrapper's ``/generate`` against the voice it already conditions on. Returns
    503 if no voice is committed. Takes the reference lock so a concurrent
    ``/test`` (which temporarily stages a candidate) can't make the audition use
    the wrong voice.
    """

    if not _reference_path().is_file():
        raise HTTPException(
            status_code=503, detail="no reference voice committed; commit one first"
        )
    async with _serialized_reference_access(settings.DATA_DIR), httpx.AsyncClient(
        timeout=settings.TTS_HTTP_TIMEOUT_SECONDS
    ) as client:
        try:
            response = await client.post(
                f"{settings.TTS_URL.rstrip('/')}/generate",
                json={"text": sample_text, "episode_id": "audition", "chunk_index": 0},
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"tts wrapper /generate failed: {exc}"
            ) from exc
        body = _read_generated_wav(response, settings)
    return Response(content=body, media_type="audio/wav")


@router.post("/commit")
async def commit_candidate(
    settings: Annotated[Settings, Depends(get_settings)],
    voice: Annotated[UploadFile, File(description="new voice clip to install (WAV/MP3/M4A/FLAC/OGG)")],
) -> dict[str, str | int | bool]:
    """Atomically replace ``backend/app/reference/voice.wav`` and ask the
    TTS wrapper to ``/reload`` so the next ``/generate`` picks it up."""

    candidate = await _read_upload_capped(voice)
    candidate, sample_rate, duration_secs = await asyncio.to_thread(
        _prepare_reference_wav, candidate
    )

    reference = _reference_path()
    reference.parent.mkdir(parents=True, exist_ok=True)

    async with _serialized_reference_access(settings.DATA_DIR):
        write_bytes_atomic(reference, candidate, prefix=".ref-")
        async with httpx.AsyncClient(
            timeout=settings.TTS_HTTP_TIMEOUT_SECONDS
        ) as client:
            try:
                await client.post(f"{settings.TTS_URL.rstrip('/')}/reload")
            except httpx.HTTPError as exc:
                # Clip is committed on disk; the wrapper just didn't reload.
                # Operator can re-trigger. Log the underlying error rather than
                # returning it so transport detail isn't exposed to the client.
                logger.warning(
                    "Reference committed but TTS /reload failed",
                    extra={"event": "reference_reload_failed", "error": str(exc)},
                )
                return {
                    "committed": True,
                    "tts_reload_warning": "voice committed but TTS reload failed; retry /commit",
                    "sample_rate": sample_rate,
                    "duration_secs": round(duration_secs),
                }
    return {
        "committed": True,
        "sample_rate": sample_rate,
        "duration_secs": round(duration_secs),
    }


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


def _restore_or_clear(reference: Path, backup: bytes | None) -> None:
    """Either restore the prior reference clip or remove the staged
    candidate when no prior clip existed."""

    try:
        if backup is not None:
            write_bytes_atomic(reference, backup, prefix=".ref-restore-")
        else:
            reference.unlink(missing_ok=True)
    except OSError:
        logger.error(
            "Reference rollback failed -- manual recovery required",
            extra={"event": "reference_rollback_failed", "path": str(reference)},
            exc_info=True,
        )


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
    write_bytes_atomic(voices.slot_path(slot), candidate, prefix=".slot-")
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
    with suppress(FileNotFoundError):
        voices.slot_path(slot).unlink()
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
    path = voices.slot_path(slot)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"voice slot {slot} is empty")
    return FileResponse(path, media_type="audio/wav")


@router.post("/slots/{slot}/audition")
async def audition_slot(
    settings: Annotated[Settings, Depends(get_settings)],
    slot: Annotated[int, PathParam(ge=1, le=voices.NUM_SLOTS)],
    sample_text: Annotated[str, Form(min_length=4, max_length=400)] = DEFAULT_SAMPLE_TEXT,
) -> Response:
    """Synthesize a sample with slot ``slot`` (selects it on the wrapper,
    generates, then restores the committed voice.wav). Mirrors /test's restore
    contract so an audition never leaves the wrapper switched to a slot."""

    if not voices.slot_path(slot).is_file():
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
            # Restore the committed voice.wav so the wrapper's resting voice
            # never leaks into a later job that resolves to the legacy default.
            await _reload_silently(client, settings)
        body = _read_generated_wav(response, settings)
    return Response(content=body, media_type="audio/wav")
