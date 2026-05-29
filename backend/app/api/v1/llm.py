"""``/api/v1/llm/models`` -- list models for the configured (or previewed) LLM
provider so the Settings UI can populate a dropdown (MinusPod pattern).

``openai-compatible``: GET ``{OPENAI_BASE_URL}/models`` -> ``.data[].id`` (the
well-known endpoint every Ollama / vLLM / LM Studio / OpenAI server exposes),
with an Ollama-native ``/api/tags`` fallback. ``anthropic``: a small hardcoded
known-model list (no cheap list endpoint). Errors never 500 -- an unreachable
or unconfigured provider returns an empty list so the UI falls back to a
free-text model field. Results are cached per (provider, base_url) with a short
TTL; ``POST /llm/models/refresh`` flushes the cache.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from app.api.deps import require_admin
from app.config import Settings, get_settings
from app.services import llm, runtime_settings

logger = logging.getLogger("app.api.v1.llm")

router = APIRouter(prefix="/llm", tags=["llm"])

# Anthropic exposes no cheap list-models endpoint; offer the current known
# model IDs as a convenience. The UI keeps a free-text fallback for anything
# newer than this list.
_ANTHROPIC_MODELS: tuple[str, ...] = (
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
)

# Per-process TTL cache keyed by "provider:base_url".
_CACHE_TTL_SECONDS = 300.0
_model_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}


class ModelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str


class ModelsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    models: list[ModelEntry]


def _cache_get(key: str) -> list[dict[str, str]] | None:
    entry = _model_cache.get(key)
    if entry is None:
        return None
    expires_at, models = entry
    if time.monotonic() >= expires_at:
        _model_cache.pop(key, None)
        return None
    return models


def _cache_set(key: str, models: list[dict[str, str]]) -> None:
    _model_cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, models)


async def _list_openai_models(
    base_url: str, api_key: str | None, extra_headers: dict[str, str] | None = None
) -> list[dict[str, str]]:
    """GET {base_url}/models -> [{id, name}], with an Ollama /api/tags fallback.

    Returns an empty list on any failure (unreachable, non-2xx, malformed body)
    so the endpoint never surfaces a provider error as a 500.
    """

    base = base_url.rstrip("/")
    endpoint = f"{base}/models"
    headers = dict(extra_headers or {})
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(endpoint, headers=headers)
            response.raise_for_status()
            data = response.json()
            ids = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
            if ids:
                return [{"id": i, "name": i} for i in ids]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.info(
                "openai-compatible /models list failed; trying Ollama fallback",
                extra={"event": "llm_models_list_failed", "detail": str(exc)},
            )
        return await _list_ollama_models(client, base)


async def _list_ollama_models(client: httpx.AsyncClient, base: str) -> list[dict[str, str]]:
    """Ollama-native fallback: GET {root}/api/tags (root = base minus /v1)."""

    root = base[:-3] if base.endswith("/v1") else base
    try:
        response = await client.get(f"{root}/api/tags")
        response.raise_for_status()
        names = [
            m["name"] for m in response.json().get("models", []) if isinstance(m, dict) and m.get("name")
        ]
        return [{"id": n, "name": n} for n in names]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.info(
            "Ollama /api/tags fallback failed",
            extra={"event": "llm_models_ollama_failed", "detail": str(exc)},
        )
        return []


def _cache_key(provider: str, base: str) -> str:
    return f"{provider}:{base}"


async def _resolve_models(settings: Settings, provider: str) -> list[dict[str, str]]:
    if provider == "anthropic":
        return [{"id": m, "name": m} for m in _ANTHROPIC_MODELS]
    # openai-compatible / openrouter / ollama all list via {base}/models.
    base, api_key, extra_headers = llm.openai_compatible_connection(settings, provider)
    base = base.rstrip("/")
    if not base:
        return []
    key = _cache_key(provider, base)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    models = await _list_openai_models(base, api_key, extra_headers)
    _cache_set(key, models)
    return models


async def _models_response(overlaid: Settings, provider: str | None) -> ModelsResponse:
    selected = provider or overlaid.LLM_PROVIDER
    models = await _resolve_models(overlaid, selected)
    return ModelsResponse(provider=selected, models=[ModelEntry(**m) for m in models])


@router.get("/models", response_model=ModelsResponse, dependencies=[Depends(require_admin)])
async def list_models(
    settings: Annotated[Settings, Depends(get_settings)],
    provider: str | None = None,
) -> ModelsResponse:
    return await _models_response(runtime_settings.overlay(settings), provider)


@router.post("/models/refresh", response_model=ModelsResponse, dependencies=[Depends(require_admin)])
async def refresh_models(
    settings: Annotated[Settings, Depends(get_settings)],
    provider: str | None = None,
) -> ModelsResponse:
    overlaid = runtime_settings.overlay(settings)
    # Drop only the entry being refreshed so other providers stay cached.
    selected = provider or overlaid.LLM_PROVIDER
    if llm.is_openai_compatible_provider(selected):
        base, _, _ = llm.openai_compatible_connection(overlaid, selected)
        _model_cache.pop(_cache_key(selected, base.rstrip("/")), None)
    return await _models_response(overlaid, provider)
