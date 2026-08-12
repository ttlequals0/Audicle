"""Client for the tts-wrapper container.

The wrapper exposes endpoints (build-plan TTS section):

- ``POST /generate`` - synthesize a single chunk; returns ``wav_path`` (in the
  shared ``/data`` volume), ``duration_secs`` and ``sample_rate``.
- ``GET /health`` - reports ``ok``, ``model_loaded``, ``reference_loaded``.
- ``POST /reload`` - re-reads the wrapper's resting voice (its lowest filled slot)
  and recomputes embeddings.
- ``POST /select-voice`` / ``POST /select-model`` - switch the reference voice
  slot / the loaded TTS model.
- ``GET /models`` - the engines the wrapper can construct, plus the active one.

Typed errors mirror the LLM client so the cleanup-stage retry classification
extends naturally to the per-chunk TTS calls:
:class:`TTSTimeoutError` and :class:`TTSProviderError` are retryable, while
:class:`TTSRequestError` (4xx, malformed response) propagates straight
through.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    wait_exponential,
)

from app.config import Settings

logger = logging.getLogger("app.services.tts")


class TTSError(Exception):
    """Base class so callers can do a single except for any TTS failure."""


class TTSTimeoutError(TTSError):
    """Request exceeded ``TTS_HTTP_TIMEOUT_SECONDS``."""


class TTSProviderError(TTSError):
    """5xx response or transport failure (unreachable, connection dropped
    mid-request by an OOM-killed wrapper). Retryable."""


class TTSUnreachableError(TTSProviderError):
    """Nothing is listening at ``TTS_URL``: the wrapper is down or restarting.

    A subclass of :class:`TTSProviderError` so existing handlers keep working,
    but distinguished because it justifies a longer retry budget than a 5xx.
    The wrapper's cold start after an OOM kill runs 60-99 s, so this is retried
    against an elapsed-time budget (``TTS_CONNECT_RETRY_MAX_SECONDS``) rather
    than the attempt count that governs other provider errors."""


class TTSRequestError(TTSError):
    """4xx response, malformed JSON, or any non-retryable failure."""


def generation_params(
    settings: Settings,
    seed: int | None = None,
    *,
    max_chars: int | None = None,
    repetition_penalty: float | None = None,
) -> dict[str, Any]:
    """Chatterbox knobs attached to every ``/generate`` call (0.44.0: the
    wrapper's env knobs are gone; these runtime settings are the single
    source). Shared by the per-chunk client below and the slot-audition
    endpoint so samples always match episode synthesis.

    Every override is the configured baseline unless a caller passes one. All
    three exist for the quality-regeneration loop: a re-gen needs a different
    seed to produce different audio, and a shorter ``max_chars`` plus a higher
    ``repetition_penalty`` to leave the failure basin rather than re-roll inside
    it (pipeline._regen_params)."""

    return {
        "temperature": settings.CHATTERBOX_TEMPERATURE,
        "repetition_penalty": (
            settings.CHATTERBOX_REPETITION_PENALTY
            if repetition_penalty is None
            else repetition_penalty
        ),
        "top_p": settings.CHATTERBOX_TOP_P,
        "top_k": settings.CHATTERBOX_TOP_K,
        "max_chars": settings.CHATTERBOX_MAX_CHARS if max_chars is None else max_chars,
        "seed": settings.CHATTERBOX_SEED if seed is None else seed,
        # Narration language rides every call; a monolingual engine 422s a
        # value it can't speak rather than ignoring it.
        "language": settings.TTS_LANGUAGE,
    }


async def _post(
    path: str,
    settings: Settings,
    payload: dict[str, Any] | None,
    what: str,
) -> httpx.Response:
    """POST to the wrapper, mapping connection-level failures to typed errors.

    ``RemoteProtocolError`` (a ``ProtocolError``, not a ``NetworkError``) is
    what a wrapper killed mid-request raises -- it must classify retryable
    like ``ConnectError``. Client-side transport errors (``UnsupportedProtocol``
    from a bad TTS_URL, ``LocalProtocolError``) stay unclassified so permanent
    misconfiguration fails fast instead of burning the retry budget. Status
    handling stays with each caller because it differs per endpoint.
    """

    endpoint = f"{settings.TTS_URL.rstrip('/')}{path}"
    timeout = httpx.Timeout(settings.TTS_HTTP_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            return await client.post(endpoint, json=payload)
        except httpx.TimeoutException as exc:
            raise TTSTimeoutError(f"{what} timed out: {exc}") from exc
        except httpx.ConnectError as exc:
            # Nothing is listening: the wrapper is down or reloading its models.
            # Gets the elapsed-time budget, not the attempt budget.
            raise TTSUnreachableError(f"TTS unreachable: {exc}") from exc
        except (httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise TTSProviderError(f"TTS unreachable: {exc}") from exc


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
    *,
    slot: int | None = None,
    max_chars: int | None = None,
    repetition_penalty: float | None = None,
) -> GenerateResult:
    """POST a single chunk to the wrapper's ``/generate`` endpoint.

    ``slot`` makes voice selection part of this call: the wrapper switches to it
    inside the same GPU lock that guards inference, so a concurrent audition
    cannot land between selecting the voice and using it."""

    payload: dict[str, Any] = {
        "text": text,
        "episode_id": episode_id,
        "chunk_index": chunk_index,
        "verify": verify,
        # The worker snapshots settings once per job, so a Settings change
        # applies to the next job; no restart.
        **generation_params(
            settings, seed, max_chars=max_chars, repetition_penalty=repetition_penalty
        ),
    }
    if slot is not None:
        payload["slot"] = slot
    response = await _post("/generate", settings, payload, "TTS call")

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
    *,
    slot: int | None = None,
    max_chars: int | None = None,
    repetition_penalty: float | None = None,
) -> GenerateResult:
    """Per-chunk TTS call with retry on transient failures.

    Build plan line 829: TTS retries happen client-side (the wrapper itself
    does not retry). ``TTS_RETRY_COUNT`` attempts with exponential backoff,
    retry only on :class:`TTSProviderError` and :class:`TTSTimeoutError`;
    :class:`TTSRequestError` propagates immediately.
    """

    return await _retry_transients(
        lambda: generate_chunk(
            text,
            episode_id,
            chunk_index,
            settings,
            seed,
            verify,
            slot=slot,
            max_chars=max_chars,
            repetition_penalty=repetition_penalty,
        ),
        settings,
    )


def _stop_policy(settings: Settings) -> Callable[[Any], bool]:
    """Stop condition that gives an unreachable wrapper a longer leash.

    A 5xx or a timeout means the wrapper answered badly, so the attempt count
    (``TTS_RETRY_COUNT``) bounds it. A ``ConnectError`` means nothing is
    listening, which after an OOM kill lasts 60-99 s while the models reload --
    longer than the attempt budget's 61 s of backoff. Those get an elapsed-time
    budget instead (``TTS_CONNECT_RETRY_MAX_SECONDS``), so a wrapper restart
    costs a job a pause rather than an hour of discarded synthesis.
    """

    def stop(state: Any) -> bool:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, TTSUnreachableError):
            return state.seconds_since_start >= settings.TTS_CONNECT_RETRY_MAX_SECONDS
        return state.attempt_number >= settings.TTS_RETRY_COUNT

    return stop


async def _retry_transients(op: Callable[[], Awaitable[Any]], settings: Settings) -> Any:
    """Run ``op`` under the shared wrapper retry policy: exponential backoff,
    retrying only provider/timeout errors. Attempt-bounded for provider errors,
    elapsed-time-bounded when the wrapper is unreachable (see ``_stop_policy``)."""

    retrying = AsyncRetrying(
        stop=_stop_policy(settings),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        retry=retry_if_exception_type((TTSProviderError, TTSTimeoutError)),
        reraise=False,
    )
    try:
        async for attempt in retrying:
            with attempt:
                return await op()
    except RetryError as exc:
        inner = exc.last_attempt.exception()
        if isinstance(inner, TTSError):
            raise inner from exc
        raise TTSProviderError(f"TTS retries exhausted: {inner}") from exc
    raise TTSProviderError("TTS retry loop exited without a response")


async def select_voice_with_retry(settings: Settings, slot: int) -> None:
    """``select_voice`` under the same retry policy as chunk generation.

    The pipeline treats voice-select failures as best-effort (log and keep the
    wrapper's current voice), so without a retry a wrapper restart that chunk
    generation rides out would silently render the episode in the wrong voice.
    """

    await _retry_transients(lambda: select_voice(settings, slot), settings)


async def _get(path: str, settings: Settings, what: str) -> httpx.Response:
    """GET from the wrapper with the same transport-error mapping as ``_post``."""

    endpoint = f"{settings.TTS_URL.rstrip('/')}{path}"
    timeout = httpx.Timeout(settings.TTS_HTTP_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            return await client.get(endpoint)
        except httpx.TimeoutException as exc:
            raise TTSTimeoutError(f"{what} timed out: {exc}") from exc
        except httpx.ConnectError as exc:
            # Nothing is listening: the wrapper is down or reloading its models.
            # Gets the elapsed-time budget, not the attempt budget.
            raise TTSUnreachableError(f"TTS unreachable: {exc}") from exc
        except (httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise TTSProviderError(f"TTS unreachable: {exc}") from exc


async def select_model(settings: Settings, model: str) -> None:
    """POST /select-model: swap the wrapper's loaded TTS model."""

    response = await _post("/select-model", settings, {"model": model}, "TTS model select")
    if response.is_server_error:
        raise TTSProviderError(
            f"model select returned {response.status_code}: {response.text[:200]}"
        )
    if response.is_client_error:
        raise TTSRequestError(
            f"model select rejected ({response.status_code}): {response.text[:200]}"
        )


async def select_model_with_retry(settings: Settings, model: str) -> None:
    """Model select with the same transient-failure retry as voice select."""

    await _retry_transients(lambda: select_model(settings, model), settings)


async def list_models(settings: Settings) -> dict[str, Any]:
    """GET /models: the engines the wrapper can construct, plus the active one."""

    response = await _get("/models", settings, "model list")
    if not response.is_success:
        raise TTSProviderError(
            f"model list returned {response.status_code}: {response.text[:200]}"
        )
    body = response.json()
    if not isinstance(body, dict):
        raise TTSRequestError(f"model list returned non-object JSON: {type(body).__name__}")
    return body


async def reload(settings: Settings) -> dict[str, Any]:
    """POST ``/reload`` on the wrapper to re-encode its resting voice (lowest filled
    slot). Used by slot auditions to restore the wrapper after a temporary switch."""

    response = await _post("/reload", settings, None, "TTS reload")
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

    response = await _post("/select-voice", settings, {"slot": slot}, "TTS select-voice")
    if response.is_error:
        raise TTSProviderError(
            f"TTS select-voice returned {response.status_code}: {response.text[:200]}"
        )
