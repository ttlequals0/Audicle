from __future__ import annotations

from pathlib import Path

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
        {"host": "washingtonpost.com", "proxy": "none", "custom_template": ""}
    ]


def test_put_rejects_bad_proxy_400(client: TestClient) -> None:
    with client:
        response = client.put(
            "/api/v1/source-fallbacks",
            json={"default_proxy": "bogus", "min_chars": 3000, "rules": []},
        )
    assert response.status_code == 400


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
