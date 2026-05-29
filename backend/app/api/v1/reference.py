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
import wave
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from app.api.deps import require_admin
from app.config import Settings, get_settings
from app.services.atomic_write import write_bytes_atomic

logger = logging.getLogger("app.api.reference")
router = APIRouter(prefix="/reference", tags=["reference"])

# Voice clip spec per build-plan: mono, 24 kHz target (16-48 acceptable),
# 6-15 s, ~150 kB-1.5 MB. The caps below are deliberate floors/ceilings
# that catch obvious mis-uploads without rejecting borderline-good clips.
_MAX_REFERENCE_BYTES = 5 * 1024 * 1024
_MIN_DURATION_SECS = 3.0
_MAX_DURATION_SECS = 60.0

# Serialises /test against itself so two operators auditioning candidates
# can't race on the shared reference path. /commit doesn't need the lock
# (atomic rename) but takes it anyway to prevent /test seeing a half-
# swapped voice.
_reference_lock = asyncio.Lock()


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


@router.get("/preview", dependencies=[Depends(require_admin)])
async def preview() -> FileResponse:
    path = _reference_path()
    if not path.is_file():
        raise HTTPException(
            status_code=404, detail="reference voice.wav not installed"
        )
    return FileResponse(path, media_type="audio/wav")


@router.post("/test", dependencies=[Depends(require_admin)])
async def test_candidate(
    settings: Annotated[Settings, Depends(get_settings)],
    voice: Annotated[UploadFile, File(description="candidate voice WAV")],
    sample_text: Annotated[
        str,
        Form(
            min_length=4,
            max_length=400,
            description="text to synthesize using the candidate clip",
        ),
    ] = "The quick brown fox jumps over the lazy dog.",
) -> Response:
    """Synthesize ``sample_text`` using ``voice`` without committing.

    Staged through the live reference path so the wrapper picks it up
    via /reload; on every exit path the committed clip is restored. If
    no clip was previously committed, the candidate is removed on exit
    so /test never silently turns into /commit.
    """

    candidate = await _read_upload_capped(voice)
    _validate_wav(candidate)

    reference = _reference_path()
    reference.parent.mkdir(parents=True, exist_ok=True)

    async with _reference_lock:
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
                if response.status_code != 200:
                    raise HTTPException(
                        status_code=502,
                        detail=f"tts wrapper /generate returned {response.status_code}",
                    )
                wav_path = Path(response.json()["wav_path"]).resolve()
                # Wrapper-supplied path is constrained to DATA_DIR so a
                # compromised or misconfigured wrapper can't trick the
                # backend into reading arbitrary files.
                data_root = Path(settings.DATA_DIR).resolve()
                if not wav_path.is_relative_to(data_root) or not wav_path.is_file():
                    raise HTTPException(
                        status_code=502,
                        detail="tts wrapper returned an invalid wav path",
                    )
                body = wav_path.read_bytes()
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


@router.post("/commit", dependencies=[Depends(require_admin)])
async def commit_candidate(
    settings: Annotated[Settings, Depends(get_settings)],
    voice: Annotated[UploadFile, File(description="new voice WAV to install")],
) -> dict[str, str | int | bool]:
    """Atomically replace ``backend/app/reference/voice.wav`` and ask the
    TTS wrapper to ``/reload`` so the next ``/generate`` picks it up."""

    candidate = await _read_upload_capped(voice)
    sample_rate, duration_secs = _validate_wav(candidate)

    reference = _reference_path()
    reference.parent.mkdir(parents=True, exist_ok=True)

    async with _reference_lock:
        write_bytes_atomic(reference, candidate, prefix=".ref-")
        async with httpx.AsyncClient(
            timeout=settings.TTS_HTTP_TIMEOUT_SECONDS
        ) as client:
            try:
                await client.post(f"{settings.TTS_URL.rstrip('/')}/reload")
            except httpx.HTTPError as exc:
                # Clip is committed on disk; the wrapper just didn't
                # reload. Operator can re-trigger.
                return {
                    "committed": True,
                    "tts_reload_warning": str(exc),
                    "sample_rate": sample_rate,
                    "duration_secs": round(duration_secs),
                }
    return {
        "committed": True,
        "sample_rate": sample_rate,
        "duration_secs": round(duration_secs),
    }


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
