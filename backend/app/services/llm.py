"""Multi-provider LLM client.

Two providers behind one interface (pattern lifted from MinusPod):

- ``openai-compatible``: POST ``{OPENAI_BASE_URL}/chat/completions``. Works
  against any service that exposes the OpenAI chat-completions wire format
  (Ollama, vLLM, LM Studio, OpenRouter, Groq, llama.cpp server, ...).
- ``anthropic``: POST ``https://api.anthropic.com/v1/messages`` with
  ``x-api-key`` + ``anthropic-version`` headers.

The cleanup pipeline wraps :func:`generate` with tenacity for retries on
:class:`LLMProviderError`; non-retryable :class:`LLMRequestError` propagates
straight through.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger("app.services.llm")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class LLMError(Exception):
    """Base class so callers can do a single except for any LLM failure."""


class LLMTimeoutError(LLMError):
    """Request exceeded ``LLM_TIMEOUT_SECONDS``."""


class LLMProviderError(LLMError):
    """5xx response from the provider. Retryable."""


class LLMRequestError(LLMError):
    """4xx response, malformed JSON, or any non-retryable failure."""


async def generate(
    system_prompt: str,
    user_message: str,
    settings: Settings,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """Send a prompt to the configured provider and return the response text.

    Per-call ``temperature`` and ``max_tokens`` override the config defaults
    when supplied.
    """

    effective_temp = temperature if temperature is not None else settings.LLM_TEMPERATURE
    effective_max = max_tokens if max_tokens is not None else settings.LLM_MAX_TOKENS
    timeout = httpx.Timeout(settings.LLM_TIMEOUT_SECONDS)

    if settings.LLM_PROVIDER == "openai-compatible":
        return await _call_openai_compatible(
            system_prompt,
            user_message,
            settings,
            temperature=effective_temp,
            max_tokens=effective_max,
            timeout=timeout,
        )
    if settings.LLM_PROVIDER == "anthropic":
        return await _call_anthropic(
            system_prompt,
            user_message,
            settings,
            temperature=effective_temp,
            max_tokens=effective_max,
            timeout=timeout,
        )
    raise LLMRequestError(f"Unknown LLM_PROVIDER={settings.LLM_PROVIDER!r}")


async def _call_openai_compatible(
    system_prompt: str,
    user_message: str,
    settings: Settings,
    *,
    temperature: float,
    max_tokens: int,
    timeout: httpx.Timeout,
) -> str:
    base = (settings.OPENAI_BASE_URL or "").rstrip("/")
    endpoint = f"{base}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if settings.OPENAI_API_KEY:
        headers["Authorization"] = f"Bearer {settings.OPENAI_API_KEY}"
    payload: dict[str, Any] = {
        "model": settings.LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    body = await _post(endpoint, headers, payload, timeout)
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise LLMRequestError(f"Unexpected openai-compatible response shape: {exc}") from exc
    # OpenAI-compatible providers may return content=null when the model
    # decides to emit tool_calls instead of text. Treat that as a request-level
    # error so the typed retry classification (LLMProviderError = retryable,
    # LLMRequestError = not) stays meaningful.
    if not isinstance(content, str):
        raise LLMRequestError(
            f"openai-compatible response contained non-string content "
            f"(type={type(content).__name__})"
        )
    return content


async def _call_anthropic(
    system_prompt: str,
    user_message: str,
    settings: Settings,
    *,
    temperature: float,
    max_tokens: int,
    timeout: httpx.Timeout,
) -> str:
    if not settings.ANTHROPIC_API_KEY:
        raise LLMRequestError("ANTHROPIC_API_KEY is not configured")
    headers = {
        "Content-Type": "application/json",
        "x-api-key": settings.ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    payload: dict[str, Any] = {
        "model": settings.LLM_MODEL,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    body = await _post(ANTHROPIC_API_URL, headers, payload, timeout)
    try:
        blocks = body["content"]
    except (KeyError, TypeError, AttributeError) as exc:
        raise LLMRequestError(f"Unexpected anthropic response shape: {exc}") from exc
    if not isinstance(blocks, list):
        raise LLMRequestError(f"Anthropic returned non-list content (type={type(blocks).__name__})")
    # Search for the first text block. Anthropic mixes thinking / tool_use /
    # citation blocks in with the response when those features are enabled,
    # so the first block isn't guaranteed to be the text we want. If multiple
    # text blocks exist, concatenate them per Anthropic's documented usage.
    texts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                texts.append(text)
    if not texts:
        raise LLMRequestError(
            f"Anthropic response contained no text blocks: {[type(b).__name__ for b in blocks]}"
        )
    return "".join(texts)


async def _post(
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: httpx.Timeout,
) -> dict[str, Any]:
    """Send the POST and map response statuses to typed exceptions.

    Returns the decoded JSON body on success; raises on every error path.
    """

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(endpoint, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"LLM call timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise LLMProviderError(f"LLM unreachable: {exc}") from exc

    if response.is_server_error:
        raise LLMProviderError(f"LLM returned {response.status_code}: {response.text[:200]}")
    if response.is_client_error:
        raise LLMRequestError(
            f"LLM rejected request ({response.status_code}): {response.text[:200]}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise LLMRequestError(f"LLM returned non-JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise LLMRequestError(f"LLM returned non-object JSON: {type(body).__name__}")
    return body
