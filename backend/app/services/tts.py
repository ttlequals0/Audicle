"""Client for the tts-wrapper container.

The wrapper exposes three endpoints (build-plan TTS section):

- ``POST /generate`` - synthesize a single chunk; returns ``wav_path`` (in the
  shared ``/data`` volume), ``duration_secs`` and ``sample_rate``.
- ``GET /health`` - reports ``ok``, ``model_loaded``, ``reference_loaded``.
- ``POST /reload`` - re-reads the wrapper's resting voice (its lowest filled slot)
  and recomputes embeddings.

Typed errors mirror the LLM client so the cleanup-stage retry classification
extends naturally to the per-chunk TTS calls:
:class:`TTSTimeoutError` and :class:`TTSProviderError` are retryable, while
:class:`TTSRequestError` (4xx, malformed response) propagates straight
through.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

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
    transcript: str | None = None
    """faster-whisper transcript of the produced audio, when ``verify`` was
    requested and the wrapper has Whisper enabled; otherwise ``None``."""


async def generate_chunk(
    text: str,
    episode_id: str,
    chunk_index: int,
    settings: Settings,
    seed: int | None = None,
    verify: bool = False,
) -> GenerateResult:
    """POST a single chunk to the wrapper's ``/generate`` endpoint."""

    endpoint = f"{settings.TTS_URL.rstrip('/')}/generate"
    payload: dict[str, Any] = {
        "text": text,
        "episode_id": episode_id,
        "chunk_index": chunk_index,
    }
    # Only attach the seed / verify flag when set, so an older wrapper
    # (extra="forbid") never receives an unexpected field.
    if seed is not None:
        payload["seed"] = seed
    if verify:
        payload["verify"] = True
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

    raw_transcript = body.get("transcript")
    try:
        result = GenerateResult(
            wav_path=str(body["wav_path"]),
            duration_secs=float(body["duration_secs"]),
            sample_rate=int(body["sample_rate"]),
            transcript=str(raw_transcript) if raw_transcript is not None else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TTSRequestError(f"Unexpected TTS response shape: {exc}") from exc
    return result


async def generate_chunk_with_retry(
    text: str,
    episode_id: str,
    chunk_index: int,
    settings: Settings,
    seed: int | None = None,
    verify: bool = False,
) -> GenerateResult:
    """Per-chunk TTS call with retry on transient failures.

    Build plan line 829: TTS retries happen client-side (the wrapper itself
    does not retry). ``TTS_RETRY_COUNT`` attempts with exponential backoff,
    retry only on :class:`TTSProviderError` and :class:`TTSTimeoutError`;
    :class:`TTSRequestError` propagates immediately.
    """

    retrying = AsyncRetrying(
        stop=stop_after_attempt(settings.TTS_RETRY_COUNT),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        retry=retry_if_exception_type((TTSProviderError, TTSTimeoutError)),
        reraise=False,
    )
    try:
        async for attempt in retrying:
            with attempt:
                return await generate_chunk(
                    text, episode_id, chunk_index, settings, seed, verify
                )
    except RetryError as exc:
        inner = exc.last_attempt.exception()
        if isinstance(inner, TTSError):
            raise inner from exc
        raise TTSProviderError(f"TTS retries exhausted: {inner}") from exc
    raise TTSProviderError("TTS retry loop exited without a response")


async def reload(settings: Settings) -> dict[str, Any]:
    """POST ``/reload`` on the wrapper to re-encode its resting voice (lowest filled
    slot). Used by slot auditions to restore the wrapper after a temporary switch."""

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


async def select_voice(settings: Settings, slot: int) -> None:
    """POST ``/select-voice`` on the wrapper to switch the per-job reference voice
    to a slot. Raises on failure; the pipeline treats it as best-effort and keeps
    the wrapper's current voice when a slot has gone missing."""

    endpoint = f"{settings.TTS_URL.rstrip('/')}/select-voice"
    timeout = httpx.Timeout(settings.TTS_HTTP_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(endpoint, json={"slot": slot})
        except httpx.TimeoutException as exc:
            raise TTSTimeoutError(f"TTS select-voice timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise TTSProviderError(f"TTS unreachable: {exc}") from exc
    if response.is_error:
        raise TTSProviderError(
            f"TTS select-voice returned {response.status_code}: {response.text[:200]}"
        )
