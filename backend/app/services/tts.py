"""Client for the tts-wrapper container.

The wrapper exposes three endpoints (build-plan TTS section):

- ``POST /generate`` - synthesize a single chunk; returns ``wav_path`` (in the
  shared ``/data`` volume), ``duration_secs`` and ``sample_rate``.
- ``GET /health`` - reports ``ok``, ``model_loaded``, ``reference_loaded``.
- ``POST /reload`` - re-reads ``reference/voice.wav`` and recomputes embeddings.

Typed errors mirror the LLM client so the cleanup-stage retry classification
extends naturally to the per-chunk TTS calls in Phase 5+:
:class:`TTSTimeoutError` and :class:`TTSProviderError` are retryable, while
:class:`TTSRequestError` (4xx, malformed response) propagates straight
through.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger("app.services.tts")


class TTSError(Exception):
    """Base class so callers can do a single except for any TTS failure."""


class TTSTimeoutError(TTSError):
    """Request exceeded ``TTS_HTTP_TIMEOUT_SECONDS``."""


class TTSProviderError(TTSError):
    """5xx response or network failure. Retryable."""


class TTSRequestError(TTSError):
    """4xx response, malformed JSON, or any non-retryable failure."""


@dataclass(frozen=True)
class GenerateResult:
    wav_path: str
    """Absolute path inside the shared ``/data`` volume."""
    duration_secs: float
    sample_rate: int


async def generate_chunk(
    text: str,
    episode_id: str,
    chunk_index: int,
    settings: Settings,
) -> GenerateResult:
    """POST a single chunk to the wrapper's ``/generate`` endpoint."""

    endpoint = f"{settings.TTS_URL.rstrip('/')}/generate"
    payload = {
        "text": text,
        "episode_id": episode_id,
        "chunk_index": chunk_index,
    }
    timeout = httpx.Timeout(settings.TTS_HTTP_TIMEOUT_SECONDS)

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(endpoint, json=payload)
        except httpx.TimeoutException as exc:
            raise TTSTimeoutError(f"TTS call timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise TTSProviderError(f"TTS unreachable: {exc}") from exc

    if response.is_server_error:
        raise TTSProviderError(f"TTS returned {response.status_code}: {response.text[:200]}")
    if response.is_client_error:
        raise TTSRequestError(
            f"TTS rejected request ({response.status_code}): {response.text[:200]}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise TTSRequestError(f"TTS returned non-JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise TTSRequestError(f"TTS returned non-object JSON: {type(body).__name__}")

    try:
        result = GenerateResult(
            wav_path=str(body["wav_path"]),
            duration_secs=float(body["duration_secs"]),
            sample_rate=int(body["sample_rate"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TTSRequestError(f"Unexpected TTS response shape: {exc}") from exc
    return result


async def reload(settings: Settings) -> dict[str, Any]:
    """POST ``/reload`` on the wrapper after a reference-voice commit."""

    endpoint = f"{settings.TTS_URL.rstrip('/')}/reload"
    timeout = httpx.Timeout(settings.TTS_HTTP_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(endpoint)
        except httpx.TimeoutException as exc:
            raise TTSTimeoutError(f"TTS reload timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise TTSProviderError(f"TTS unreachable: {exc}") from exc
    if response.is_server_error:
        raise TTSProviderError(f"TTS reload returned {response.status_code}: {response.text[:200]}")
    if response.is_client_error:
        raise TTSRequestError(
            f"TTS reload rejected ({response.status_code}): {response.text[:200]}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise TTSRequestError(f"TTS reload returned non-JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise TTSRequestError(f"TTS reload returned non-object JSON: {type(body).__name__}")
    return body
