"""OpenAI-compatible TTS and ASR clients, so Chatterbox can run on another host.

Issue #117. The bundled wrapper and a remote server differ in one structural way
that shapes this whole module: the wrapper returns ``wav_path`` pointing into the
``/data`` volume it SHARES with this app, while a remote host shares no
filesystem. So the remote path receives audio bytes and writes them to the path
the pipeline expects, producing the same :class:`~app.services.tts.GenerateResult`
the wrapper path does. Everything downstream (audio analysis, concat, chapters)
already works from that file and needs no knowledge of which backend ran.

The second consequence is verification. The wrapper transcribes what it just
synthesized and hands back a transcript; a remote speech endpoint returns only
audio. So ASR becomes a separate call here, against an OpenAI-compatible
``/v1/audio/transcriptions``. That is also why the two backends are configured
independently: local synthesis with remote ASR is a legitimate pairing.

Operator-supplied URLs are fetched server-side, so both calls go through the
shared SSRF guard rather than trusting the configured host.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from app.config import Settings
from app.core.paths import media_dir
from app.services import ssrf
from app.services.atomic_write import write_bytes_atomic

logger = logging.getLogger("app.services.tts_remote")

_GUARD_TTL_SECONDS = 300.0
_guard_cache: dict[str, float] = {}


def _auth_headers(api_key: str) -> dict[str, str]:
    key = api_key.strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


async def _guard(url: str) -> None:
    """Run the SSRF guard over an operator-supplied endpoint.

    Both endpoints are configured through the Settings UI, so a hostile or
    mistaken value could otherwise point the app at an internal address.

    Verdicts are cached per host for a short TTL: the guard re-resolves DNS on
    every chunk otherwise. A changed endpoint host misses the cache by key, so
    Settings edits take effect immediately; the TTL only bounds how long a
    previously approved host is trusted."""

    host = httpx.URL(url).host
    if not host:
        return
    now = time.monotonic()
    expiry = _guard_cache.get(host)
    if expiry is not None and now < expiry:
        return
    await ssrf.resolve_public_host(host)
    _guard_cache[host] = now + _GUARD_TTL_SECONDS


async def synthesize(
    text: str,
    episode_id: str,
    chunk_index: int,
    settings: Settings,
) -> tuple[Path, bytes]:
    """POST to an OpenAI-compatible ``/v1/audio/speech`` and store the audio.

    Returns the written path and the raw bytes (the caller needs the bytes for
    duration and for a remote ASR call, and re-reading the file to get them back
    would be wasteful).

    Errors are raised as the same typed exceptions the wrapper client uses, so
    the pipeline's retry classification applies unchanged to this backend.
    """

    from app.services.tts import (
        TTSProviderError,
        TTSRequestError,
        TTSTimeoutError,
        TTSUnreachableError,
        shared_client,
    )

    base = settings.TTS_API_BASE_URL.rstrip("/")
    endpoint = f"{base}/audio/speech"
    await _guard(endpoint)
    payload = {
        "model": settings.TTS_API_MODEL,
        "input": text,
        "voice": settings.TTS_API_VOICE,
        # WAV keeps the pipeline's existing assumptions: the audio stage reads
        # PCM WAV chunks and concatenates them.
        "response_format": "wav",
    }
    timeout = httpx.Timeout(settings.TTS_HTTP_TIMEOUT_SECONDS)
    client = shared_client()
    try:
        response = await client.post(
            endpoint,
            json=payload,
            headers=_auth_headers(settings.TTS_API_KEY),
            timeout=timeout,
        )
    except httpx.TimeoutException as exc:
        raise TTSTimeoutError(f"remote TTS timed out: {exc}") from exc
    except httpx.ConnectError as exc:
        raise TTSUnreachableError(f"TTS unreachable: {exc}") from exc
    except (httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        raise TTSProviderError(f"TTS unreachable: {exc}") from exc

    if response.is_server_error:
        raise TTSProviderError(f"remote TTS returned {response.status_code}")
    if response.is_client_error:
        raise TTSRequestError(
            f"remote TTS rejected request ({response.status_code}): {response.text[:200]}"
        )

    audio = response.content
    if not audio:
        raise TTSProviderError("remote TTS returned an empty body")

    out_dir = media_dir(settings)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Same filename the wrapper would have written, so the pipeline's chunk
    # bookkeeping and cleanup are identical across backends.
    wav_path = out_dir / f"{episode_id}_chunk_{chunk_index}.wav"
    write_bytes_atomic(wav_path, audio)
    return wav_path, audio


async def transcribe(audio: bytes, settings: Settings) -> str | None:
    """POST audio to an OpenAI-compatible ``/v1/audio/transcriptions``.

    Returns None on any failure. Verification is a quality gate, not a
    correctness requirement: a transcription outage must degrade to "no
    transcript for this chunk" exactly as the wrapper path does, never fail a
    chunk that synthesized fine.
    """

    from app.services.tts import shared_client

    base = settings.WHISPER_API_BASE_URL.rstrip("/")
    endpoint = f"{base}/audio/transcriptions"
    try:
        await _guard(endpoint)
    except Exception as exc:
        logger.warning(
            "Remote ASR endpoint rejected by the SSRF guard",
            extra={"event": "whisper_remote_blocked", "error": str(exc)},
        )
        return None

    timeout = httpx.Timeout(settings.WHISPER_API_TIMEOUT_SECONDS)
    try:
        client = shared_client()
        response = await client.post(
            endpoint,
            headers=_auth_headers(settings.WHISPER_API_KEY),
            files={"file": ("chunk.wav", audio, "audio/wav")},
            data={"model": settings.WHISPER_API_MODEL, "response_format": "json"},
            timeout=timeout,
        )
        if response.status_code >= 400:
            logger.warning(
                "Remote ASR verification failed; continuing without a transcript",
                extra={
                    "event": "whisper_remote_error",
                    "status": response.status_code,
                    "body": response.text[:200],
                },
            )
            return None
        body = response.json()
    except Exception as exc:
        logger.warning(
            "Remote ASR verification failed; continuing without a transcript",
            extra={"event": "whisper_remote_error", "error": str(exc)},
        )
        return None

    if isinstance(body, dict):
        text = body.get("text")
        if isinstance(text, str):
            return text
    logger.warning(
        "Remote ASR returned an unexpected shape; continuing without a transcript",
        extra={"event": "whisper_remote_error", "shape": type(body).__name__},
    )
    return None
