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
from collections.abc import Awaitable, Callable
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.services import llm, runtime_settings

logger = logging.getLogger("app.api.v1.llm")

router = APIRouter(prefix="/llm", tags=["llm"])

ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
OPENROUTER_AUTH_URL = "https://openrouter.ai/api/v1/auth/key"

# Errors tolerated when listing models: a provider failure degrades to a fallback
# / empty list rather than surfacing as a 500.
_MODEL_LIST_ERRORS = (httpx.HTTPError, ValueError, KeyError, TypeError)

# Static fallback when the live Anthropic /v1/models list can't be fetched
# (no key, network error). The UI also keeps a free-text field for anything
# newer than whatever the list returns.
_ANTHROPIC_MODELS: tuple[str, ...] = (
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
)


async def _list_anthropic_models(api_key: str | None) -> list[dict[str, str]]:
    """GET Anthropic's live /v1/models, falling back to the static IDs.

    Anthropic does expose a models list (paginated; ``data[].id`` +
    ``display_name``); request a large page so the dropdown isn't truncated.
    """

    static = [{"id": m, "name": m} for m in _ANTHROPIC_MODELS]
    if not api_key:
        return static
    headers = {"x-api-key": api_key, "anthropic-version": llm.ANTHROPIC_VERSION}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                ANTHROPIC_MODELS_URL, params={"limit": 1000}, headers=headers
            )
            response.raise_for_status()
            data = response.json().get("data", [])
        models = [
            {"id": m["id"], "name": m.get("display_name") or m["id"]}
            for m in data
            if isinstance(m, dict) and m.get("id")
        ]
        return models or static
    except _MODEL_LIST_ERRORS as exc:
        logger.warning(
            "anthropic /models list failed; using static list",
            extra={"event": "llm_anthropic_models_failed", "detail": str(exc)},
        )
        return static


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


class ConnectionTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    base_url: str | None = None
    api_key: str | None = Field(default=None, max_length=4096)


class ConnectionTestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    reachable: bool
    status: int | None = None
    detail: str


def _sorted_models(models: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(models, key=lambda model: (model["name"].casefold(), model["id"].casefold()))


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
        except _MODEL_LIST_ERRORS as exc:
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
            m["name"]
            for m in response.json().get("models", [])
            if isinstance(m, dict) and m.get("name")
        ]
        return [{"id": n, "name": n} for n in names]
    except _MODEL_LIST_ERRORS as exc:
        logger.info(
            "Ollama /api/tags fallback failed",
            extra={"event": "llm_models_ollama_failed", "detail": str(exc)},
        )
        return []


def _cache_key(provider: str, base: str) -> str:
    return f"{provider}:{base}"


_ANTHROPIC_CACHE_KEY = "anthropic:api"


async def _cached(
    key: str, fetch: Callable[[], Awaitable[list[dict[str, str]]]]
) -> list[dict[str, str]]:
    """Return the cached model list for ``key``, else fetch, cache, and return."""

    cached = _cache_get(key)
    if cached is not None:
        return cached
    models = await fetch()
    _cache_set(key, models)
    return models


async def _resolve_models(settings: Settings, provider: str) -> list[dict[str, str]]:
    if provider == "anthropic":
        return await _cached(
            _ANTHROPIC_CACHE_KEY, lambda: _list_anthropic_models(settings.ANTHROPIC_API_KEY)
        )
    # openai-compatible / openrouter / ollama all list via {base}/models.
    base, api_key, extra_headers = llm.openai_compatible_connection(settings, provider)
    base = base.rstrip("/")
    if not base:
        return []
    return await _cached(
        _cache_key(provider, base), lambda: _list_openai_models(base, api_key, extra_headers)
    )


async def _models_response(overlaid: Settings, provider: str | None) -> ModelsResponse:
    selected = provider or overlaid.LLM_PROVIDER
    models = _sorted_models(await _resolve_models(overlaid, selected))
    return ModelsResponse(provider=selected, models=[ModelEntry(**m) for m in models])


def _same_origin(left: str, right: str) -> bool:
    try:
        a, b = httpx.URL(left), httpx.URL(right)
        return (a.scheme, a.host, a.port) == (b.scheme, b.host, b.port)
    except ValueError:
        return False


def _provider_url(url: str) -> httpx.URL:
    parsed = httpx.URL(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.host
        or parsed.userinfo
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid provider URL")
    return parsed


def _draft_key(request: ConnectionTestRequest, saved: str | None, same_origin: bool) -> str | None:
    if (
        "api_key" not in request.model_fields_set
        or request.api_key == runtime_settings.MASK_SENTINEL
    ):
        return saved if same_origin else None
    return request.api_key or None


def provider_probe_request(
    settings: Settings, request: ConnectionTestRequest
) -> tuple[str, dict[str, str], str]:
    provider = request.provider
    if provider == "anthropic":
        key = _draft_key(request, settings.ANTHROPIC_API_KEY, True)
        return (
            ANTHROPIC_MODELS_URL,
            {
                **({"x-api-key": key} if key else {}),
                "anthropic-version": llm.ANTHROPIC_VERSION,
            },
            "anthropic",
        )
    if provider == "openrouter":
        _base, saved_key, _extra = llm.openai_compatible_connection(settings, provider)
        key = _draft_key(request, saved_key, True)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return OPENROUTER_AUTH_URL, headers, "openrouter"
    elif provider == "ollama":
        base = (
            request.base_url if "base_url" in request.model_fields_set else settings.OLLAMA_BASE_URL
        )
        base = base or ""
        key, extra = None, {}
    elif provider == "openai-compatible":
        saved_base, saved_key, extra = llm.openai_compatible_connection(settings, provider)
        base = request.base_url if "base_url" in request.model_fields_set else saved_base
        base = base or ""
        key = _draft_key(request, saved_key, _same_origin(base, saved_base))
    else:
        raise ValueError("unsupported provider")
    headers = dict(extra)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    parsed = _provider_url(base)
    if provider == "ollama":
        root = str(parsed).rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        return f"{root}/api/tags", headers, "ollama"
    return f"{str(parsed).rstrip('/')}/models", headers, "openai"


def valid_provider_probe_response(kind: str, response: httpx.Response) -> bool:
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    if kind == "openrouter":
        return isinstance(payload.get("data"), dict)
    field = "models" if kind == "ollama" else "data"
    return isinstance(payload.get(field), list)


@router.get("/models", response_model=ModelsResponse)
async def list_models(
    settings: Annotated[Settings, Depends(get_settings)],
    provider: str | None = None,
) -> ModelsResponse:
    return await _models_response(runtime_settings.overlay(settings), provider)


@router.post("/models/refresh", response_model=ModelsResponse)
async def refresh_models(
    settings: Annotated[Settings, Depends(get_settings)],
    provider: str | None = None,
) -> ModelsResponse:
    overlaid = runtime_settings.overlay(settings)
    # Drop only the entry being refreshed so other providers stay cached.
    selected = provider or overlaid.LLM_PROVIDER
    if selected == "anthropic":
        _model_cache.pop(_ANTHROPIC_CACHE_KEY, None)
    elif llm.is_openai_compatible_provider(selected):
        base, _, _ = llm.openai_compatible_connection(overlaid, selected)
        _model_cache.pop(_cache_key(selected, base.rstrip("/")), None)
    return await _models_response(overlaid, provider)


@router.post("/test", response_model=ConnectionTestResponse)
async def test_provider(
    body: ConnectionTestRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ConnectionTestResponse:
    overlaid = runtime_settings.overlay(settings)
    try:
        endpoint, headers, kind = provider_probe_request(overlaid, body)
    except ValueError:
        return ConnectionTestResponse(
            ok=False, reachable=False, detail="Unsupported provider or invalid URL"
        )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(endpoint, headers=headers)
    except (httpx.HTTPError, ValueError):
        return ConnectionTestResponse(
            ok=False, reachable=False, detail="Provider could not be reached"
        )
    if response.is_success and valid_provider_probe_response(kind, response):
        return ConnectionTestResponse(
            ok=True, reachable=True, status=response.status_code, detail="Connection succeeded"
        )
    detail = (
        "Provider rejected the credentials"
        if response.status_code in {401, 403}
        else "Provider returned an invalid response"
        if response.is_success
        else "Provider returned an error"
    )
    return ConnectionTestResponse(
        ok=False, reachable=True, status=response.status_code, detail=detail
    )
