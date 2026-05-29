from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from app.api.v1 import llm
from app.core import database
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _clear_model_cache():
    llm._model_cache.clear()
    yield
    llm._model_cache.clear()


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _transport(*responses) -> httpx.MockTransport:
    iterator = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        value = next(iterator)
        if isinstance(value, Exception):
            raise value
        return value

    return httpx.MockTransport(handler)


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def test_anthropic_returns_known_model_list(env: Path) -> None:
    with _client(env) as client:
        response = client.get("/api/v1/llm/models?provider=anthropic")
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "anthropic"
    ids = [m["id"] for m in body["models"]]
    assert ids == list(llm._ANTHROPIC_MODELS)


def test_openai_compatible_lists_models_from_endpoint(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_async_client(
        monkeypatch,
        _transport(
            httpx.Response(200, json={"data": [{"id": "qwen3"}, {"id": "mistral"}]}),
        ),
    )
    with _client(env) as client:
        response = client.get("/api/v1/llm/models")
    assert response.status_code == 200
    ids = [m["id"] for m in response.json()["models"]]
    assert ids == ["qwen3", "mistral"]


def test_openrouter_lists_models(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_async_client(
        monkeypatch,
        _transport(httpx.Response(200, json={"data": [{"id": "anthropic/claude-3.5"}]})),
    )
    with _client(env) as client:
        response = client.get("/api/v1/llm/models?provider=openrouter")
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "openrouter"
    assert [m["id"] for m in body["models"]] == ["anthropic/claude-3.5"]


def test_ollama_lists_models(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_async_client(
        monkeypatch,
        _transport(httpx.Response(200, json={"data": [{"id": "llama3:8b"}]})),
    )
    with _client(env) as client:
        response = client.get("/api/v1/llm/models?provider=ollama")
    assert response.status_code == 200
    assert [m["id"] for m in response.json()["models"]] == ["llama3:8b"]


def test_openai_compatible_falls_back_to_ollama_tags(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-OpenAI /models (404) falls through to Ollama's /api/tags."""

    _patch_async_client(
        monkeypatch,
        _transport(
            httpx.Response(404, text="not found"),  # /models
            httpx.Response(200, json={"models": [{"name": "llama3:8b"}]}),  # /api/tags
        ),
    )
    with _client(env) as client:
        response = client.get("/api/v1/llm/models")
    assert response.status_code == 200
    assert [m["id"] for m in response.json()["models"]] == ["llama3:8b"]


def test_unreachable_provider_returns_empty_list_not_500(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_async_client(
        monkeypatch,
        _transport(
            httpx.ConnectError("refused"),  # /models
            httpx.ConnectError("refused"),  # /api/tags fallback
        ),
    )
    with _client(env) as client:
        response = client.get("/api/v1/llm/models")
    assert response.status_code == 200
    assert response.json()["models"] == []


def test_refresh_bypasses_cache(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first GET caches; refresh clears the cache and re-fetches the new
    list from the endpoint."""

    _patch_async_client(
        monkeypatch,
        _transport(
            httpx.Response(200, json={"data": [{"id": "old-model"}]}),  # GET
            httpx.Response(200, json={"data": [{"id": "new-model"}]}),  # refresh
        ),
    )
    with _client(env) as client:
        first = client.get("/api/v1/llm/models")
        assert [m["id"] for m in first.json()["models"]] == ["old-model"]
        # A second GET would hit the cache; refresh must re-fetch.
        refreshed = client.post("/api/v1/llm/models/refresh")
        assert [m["id"] for m in refreshed.json()["models"]] == ["new-model"]
