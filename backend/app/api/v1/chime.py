"""``/api/v1/chime`` -- the optional end-of-episode chime clip.

A single operator-uploaded short clip, transcoded to ``{media}/chime.wav`` (24 kHz mono,
the episode working format). When ``CHIME_ENABLED`` is on and the clip exists, the audio
stage appends it to the end of every episode so back-to-back episodes are
distinguishable. Admin-gated by the ``/api/v1`` router group.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse

from app.api.v1.uploads import read_upload_capped
from app.config import Settings, get_settings
from app.core.paths import media_dir
from app.services import audio
from app.services.atomic_write import write_bytes_atomic

router = APIRouter(tags=["chime"])

# Cap the upload before buffering it; a chime is a few seconds, so this is generous.
_MAX_CHIME_BYTES = 10 * 1024 * 1024
# Transcode caps the stored clip's length so an over-long upload can't pad every episode.
_MAX_CHIME_SECONDS = 15


def _chime_path(settings: Settings) -> Path:
    return media_dir(settings) / "chime.wav"


def _status(settings: Settings) -> dict[str, Any]:
    path = _chime_path(settings)
    if not path.is_file():
        return {"present": False, "duration_secs": None}
    try:
        duration = round(audio.wav_duration_secs(path))
    except Exception:
        # An unreadable clip (truncated by an external process) still reads as present;
        # don't 500 the status endpoint over a duration we can't compute.
        duration = None
    return {"present": True, "duration_secs": duration}


@router.get("/chime", summary="Whether an end-of-episode chime clip is uploaded")
def get_chime(settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, Any]:
    return _status(settings)


@router.post("/chime", status_code=201, summary="Upload the end-of-episode chime clip")
async def upload_chime(
    file: Annotated[UploadFile, File(description="short audio clip (WAV/MP3/M4A/FLAC/OGG)")],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    data = await read_upload_capped(file, _MAX_CHIME_BYTES)
    if not data:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    try:
        wav = await asyncio.to_thread(audio.transcode_to_wav, data, max_seconds=_MAX_CHIME_SECONDS)
    except audio.AudioError as exc:
        # The ffmpeg reason is logged inside transcode; the client gets a fixed message.
        raise HTTPException(status_code=400, detail="could not decode the audio clip") from exc
    write_bytes_atomic(_chime_path(settings), wav, prefix=".chime-")
    return _status(settings)


@router.get("/chime/preview", summary="Play the uploaded chime clip")
def preview_chime(settings: Annotated[Settings, Depends(get_settings)]) -> FileResponse:
    path = _chime_path(settings)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no chime uploaded")
    return FileResponse(path, media_type="audio/wav")


@router.delete("/chime", status_code=204, summary="Remove the end-of-episode chime clip")
def delete_chime(settings: Annotated[Settings, Depends(get_settings)]) -> Response:
    with contextlib.suppress(FileNotFoundError):
        _chime_path(settings).unlink()
    return Response(status_code=204)
