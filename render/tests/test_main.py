from __future__ import annotations

from fastapi.testclient import TestClient

from main import create_app
from renderer import RenderResult


class FakeRenderer:
    """Stand-in for CamoufoxRenderer; records calls, returns a canned result.
    Never launches a browser."""

    def __init__(self, result: RenderResult) -> None:
        self.result = result
        self.calls: list[tuple[str, bool]] = []

    async def render(self, url: str, expand: bool) -> RenderResult:
        self.calls.append((url, expand))
        return self.result


def _client(result: RenderResult) -> tuple[TestClient, FakeRenderer]:
    renderer = FakeRenderer(result)
    return TestClient(create_app(renderer=renderer)), renderer


def test_health_live_reports_ok_and_version() -> None:
    client, _ = _client(RenderResult(status="ok"))
    response = client.get("/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "version" in body


def test_render_returns_the_renderers_result() -> None:
    client, renderer = _client(
        RenderResult(status="ok", html="<html>full</html>", clicks=2, word_estimate=770)
    )
    response = client.post("/render", json={"url": "https://www.inc.com/x", "expand": True})
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "ok",
        "html": "<html>full</html>",
        "clicks": 2,
        "word_estimate": 770,
    }
    assert renderer.calls == [("https://www.inc.com/x", True)]


def test_render_defaults_expand_true() -> None:
    client, renderer = _client(RenderResult(status="ok"))
    client.post("/render", json={"url": "https://www.inc.com/x"})
    assert renderer.calls == [("https://www.inc.com/x", True)]


def test_render_passes_through_captcha_status() -> None:
    client, _ = _client(RenderResult(status="captcha", clicks=1, word_estimate=12))
    response = client.post("/render", json={"url": "https://www.inc.com/x"})
    assert response.json()["status"] == "captcha"
    assert response.json()["html"] == ""
