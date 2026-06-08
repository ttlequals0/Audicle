from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from app.core import database
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def test_get_defaults_with_available_proxies_and_builtin(client: TestClient) -> None:
    with client:
        body = client.get("/api/v1/source-fallbacks").json()
    assert body["default_proxy"] == "googlebot"
    assert body["min_chars"] == 3000
    assert body["rules"] == []
    assert {p["key"] for p in body["available_proxies"]} == {
        "googlebot",
        "freedium",
        "custom",
        "none",
        "flaresolverr",
        "archive",
    }
    assert any(b["host"] == "medium.com" for b in body["builtin"])


def test_put_round_trips_and_normalizes(client: TestClient) -> None:
    with client:
        put = client.put(
            "/api/v1/source-fallbacks",
            json={
                "default_proxy": "googlebot",
                "min_chars": 3500,
                "rules": [{"host": "WashingtonPost.com", "proxy": "none"}],
            },
        )
        assert put.status_code == 200
        got = client.get("/api/v1/source-fallbacks").json()
    assert got["min_chars"] == 3500
    assert got["rules"] == [
        {"host": "washingtonpost.com", "proxy": "none", "custom_template": "", "cookies": ""}
    ]


def test_put_rejects_bad_proxy_400(client: TestClient) -> None:
    with client:
        response = client.put(
            "/api/v1/source-fallbacks",
            json={"default_proxy": "bogus", "min_chars": 3000, "rules": []},
        )
    assert response.status_code == 400


def test_cookies_masked_on_read_and_preserved_on_resave(client: TestClient, env: Path) -> None:
    from app.services import source_fallbacks_store

    rule = {"host": "nytimes.com", "proxy": "flaresolverr", "cookies": "sess=secret123"}
    with client:
        client.put(
            "/api/v1/source-fallbacks",
            json={"default_proxy": "googlebot", "min_chars": 3000, "rules": [rule]},
        )
        got = client.get("/api/v1/source-fallbacks").json()
        masked = got["rules"][0]["cookies"]
        assert masked == "********"  # never echo the real value
        # Re-saving with the sentinel keeps the stored cookies (the UI never saw them).
        client.put(
            "/api/v1/source-fallbacks",
            json={
                "default_proxy": "googlebot",
                "min_chars": 3000,
                "rules": [{"host": "nytimes.com", "proxy": "flaresolverr", "cookies": masked}],
            },
        )
    with database.connection(env) as conn:
        stored = source_fallbacks_store.load(conn)
    assert stored["rules"][0]["cookies"] == "sess=secret123"  # real value survived


def test_cookies_can_be_cleared_with_empty_string(client: TestClient, env: Path) -> None:
    from app.services import source_fallbacks_store

    with client:
        client.put(
            "/api/v1/source-fallbacks",
            json={
                "default_proxy": "googlebot",
                "min_chars": 3000,
                "rules": [{"host": "nytimes.com", "proxy": "flaresolverr", "cookies": "sess=x"}],
            },
        )
        # Sending "" (not the sentinel) clears the cookies.
        client.put(
            "/api/v1/source-fallbacks",
            json={
                "default_proxy": "googlebot",
                "min_chars": 3000,
                "rules": [{"host": "nytimes.com", "proxy": "flaresolverr", "cookies": ""}],
            },
        )
    with database.connection(env) as conn:
        stored = source_fallbacks_store.load(conn)
    assert stored["rules"][0]["cookies"] == ""


def test_put_rejects_flaresolverr_as_global_default_400(client: TestClient) -> None:
    # flaresolverr is a per-host remedy, not a global default (it would route every
    # below-floor scrape through the browser solve). Allowed per-host, blocked global.
    with client:
        response = client.put(
            "/api/v1/source-fallbacks",
            json={"default_proxy": "flaresolverr", "min_chars": 3000, "rules": []},
        )
    assert response.status_code == 400


def test_put_accepts_flaresolverr_as_per_host_rule(client: TestClient) -> None:
    with client:
        response = client.put(
            "/api/v1/source-fallbacks",
            json={
                "default_proxy": "googlebot",
                "min_chars": 3000,
                "rules": [{"host": "nytimes.com", "proxy": "flaresolverr", "custom_template": ""}],
            },
        )
    assert response.status_code == 200
    assert response.json()["rules"][0]["proxy"] == "flaresolverr"


def test_put_rejects_custom_without_placeholder_400(client: TestClient) -> None:
    with client:
        response = client.put(
            "/api/v1/source-fallbacks",
            json={
                "default_proxy": "googlebot",
                "min_chars": 3000,
                "rules": [{"host": "x.com", "proxy": "custom", "custom_template": "https://no/"}],
            },
        )
    assert response.status_code == 400


def test_test_endpoint_reports_chars_without_leaking_cookies(
    client: TestClient, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stub Firecrawl so the test endpoint's extract() returns a full article with no
    # network call; the response must report chars/strategy but never the cookie value.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": True, "data": {"markdown": "word " * 800, "metadata": {"title": "T"}}},
        )

    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: original(*a, **{**k, "transport": transport})
    )
    with client:
        client.put(
            "/api/v1/source-fallbacks",
            json={
                "default_proxy": "googlebot",
                "min_chars": 3000,
                "rules": [
                    {"host": "gated.test", "proxy": "flaresolverr", "cookies": "sess=secret123"}
                ],
            },
        )
        resp = client.post("/api/v1/source-fallbacks/test", json={"url": "https://gated.test/a"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["chars"] > 3000
    assert body["strategy"] == "flaresolverr"
    assert "secret123" not in resp.text  # the cookie value is never echoed


def test_test_endpoint_requires_a_url(client: TestClient) -> None:
    with client:
        resp = client.post("/api/v1/source-fallbacks/test", json={})
    assert resp.status_code == 400


def test_test_endpoint_rejects_non_http_url(client: TestClient) -> None:
    # file://, gopher://, etc. are rejected before reaching Firecrawl/the solver.
    with client:
        resp = client.post("/api/v1/source-fallbacks/test", json={"url": "file:///etc/passwd"})
    assert resp.status_code == 400
