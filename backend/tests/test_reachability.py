from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from app.config import get_settings
from app.services import reachability


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _transport(*responses) -> httpx.MockTransport:
    iterator = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            value = next(iterator)
        except StopIteration as exc:
            raise AssertionError("Reachability made more HTTP calls than expected") from exc
        if isinstance(value, Exception):
            raise value
        return value

    return httpx.MockTransport(handler)


async def test_check_firecrawl_ok_on_2xx(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_async_client(monkeypatch, _transport(httpx.Response(200, text='{"ok":true}')))
    result = await reachability.check_firecrawl(get_settings())
    assert result.ok is True
    assert "200" in result.detail


async def test_check_firecrawl_falls_through_to_root_on_404(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Self-hosted Firecrawl may not expose /v1/health; the prober tries the
    fallback endpoints and reports the first 2xx."""

    _patch_async_client(
        monkeypatch,
        _transport(
            httpx.Response(404, text="not found"),
            httpx.Response(404, text="not found"),
            httpx.Response(200, text="root ok"),
        ),
    )
    result = await reachability.check_firecrawl(get_settings())
    assert result.ok is True


async def test_check_firecrawl_reports_unreachable_on_network_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_async_client(
        monkeypatch,
        _transport(
            httpx.ConnectError("connect refused"),
            httpx.ConnectError("connect refused"),
            httpx.ConnectError("connect refused"),
        ),
    )
    result = await reachability.check_firecrawl(get_settings())
    assert result.ok is False
    assert "unreachable" in result.detail


async def test_check_firecrawl_reports_last_failure_when_all_endpoints_5xx(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_async_client(
        monkeypatch,
        _transport(
            httpx.Response(502, text="bad gateway 1"),
            httpx.Response(502, text="bad gateway 2"),
            httpx.Response(502, text="bad gateway 3"),
        ),
    )
    result = await reachability.check_firecrawl(get_settings())
    assert result.ok is False
    assert "502" in result.detail


async def test_check_tts_ok_when_model_loaded_even_on_503(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrapper with the model up but no reference voice yet returns 503; it is
    still reachable (the operator uploads a voice via the UI), so the worker
    must not block on it."""

    _patch_async_client(
        monkeypatch,
        _transport(
            httpx.Response(503, json={"model_loaded": True, "reference_loaded": False}),
        ),
    )
    result = await reachability.check_tts(get_settings())
    assert result.ok is True
    assert "model_loaded=true" in result.detail


async def test_run_all_is_advisory_and_never_raises_when_a_check_fails(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A down dependency is logged + surfaced via /health/ready but must not
    raise -- startup is de-gated (the worker enters its poll loop regardless)."""

    _patch_async_client(
        monkeypatch,
        _transport(
            httpx.ConnectError("nope"),  # firecrawl /v1/health
            httpx.ConnectError("nope"),  # firecrawl /health
            httpx.ConnectError("nope"),  # firecrawl /
            httpx.Response(200, text='{"data": []}'),  # llm /models
            httpx.Response(
                200, json={"ok": True, "model_loaded": True, "reference_loaded": True}
            ),  # tts /health
        ),
    )
    results = await reachability.run_all(get_settings())
    by_name = {r.name: r for r in results}
    assert by_name["firecrawl"].ok is False
    assert by_name["llm"].ok is True
    assert by_name["tts"].ok is True


async def test_run_all_returns_results_when_all_pass(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_async_client(
        monkeypatch,
        _transport(
            httpx.Response(200, text="ok"),  # firecrawl
            httpx.Response(200, text='{"data": []}'),  # llm /models
            httpx.Response(
                200, json={"ok": True, "model_loaded": True, "reference_loaded": True}
            ),  # tts /health
        ),
    )
    results = await reachability.run_all(get_settings())
    assert all(r.ok for r in results)
    assert {r.name for r in results} == {"firecrawl", "llm", "tts"}
