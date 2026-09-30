from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from camoufox_renderer import CamoufoxRenderer

from log_config import TextFormatter
from main import create_app
from renderer import RenderOptions, RenderResult


class FakeRenderer:
    """Stand-in for CamoufoxRenderer; records calls, returns a canned result.
    Never launches a browser."""

    def __init__(self, result: RenderResult) -> None:
        self.result = result
        self.calls: list[tuple[str, bool]] = []
        self.cookies: list[str | None] = []
        self.budgets: list[float | None] = []
        self.options: list[RenderOptions | None] = []

    async def render(
        self,
        url: str,
        expand: bool,
        email: str | None = None,
        cookies: str | None = None,
        budget_seconds: float | None = None,
        options: RenderOptions | None = None,
    ) -> RenderResult:
        self.calls.append((url, expand))
        self.cookies.append(cookies)
        self.budgets.append(budget_seconds)
        self.options.append(options)
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


# --- registration email pass-through (0.53.0) ------------------------------


class FakeEmailRenderer:
    """Records the email the sidecar was asked to use."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def render(
        self,
        url: str,
        expand: bool,
        email: str | None = None,
        cookies: str | None = None,
        budget_seconds: float | None = None,
        options: RenderOptions | None = None,
    ) -> RenderResult:
        self.calls.append({"url": url, "expand": expand, "email": email})
        return RenderResult(status="ok", html="<html>unlocked</html>", word_estimate=900)


def test_render_forwards_the_registration_email() -> None:
    renderer = FakeEmailRenderer()
    client = TestClient(create_app(renderer=renderer))
    response = client.post(
        "/render", json={"url": "https://w42st.com/post/x", "email": "reader@example.test"}
    )
    assert response.status_code == 200
    assert renderer.calls[0]["email"] == "reader@example.test"


def test_render_without_an_email_passes_none() -> None:
    renderer = FakeEmailRenderer()
    client = TestClient(create_app(renderer=renderer))
    client.post("/render", json={"url": "https://w42st.com/post/x"})
    assert renderer.calls[0]["email"] is None


def test_render_forwards_the_cookie_jar() -> None:
    client, renderer = _client(RenderResult(status="ok"))
    client.post("/render", json={"url": "https://www.wsj.com/x", "cookies": "sid=abc; t=1"})
    client.post("/render", json={"url": "https://www.wsj.com/x"})
    assert renderer.cookies == ["sid=abc; t=1", None]


def test_render_forwards_the_callers_budget() -> None:
    client, renderer = _client(RenderResult(status="ok"))
    client.post("/render", json={"url": "https://example.com/a", "budget_seconds": 140})
    client.post("/render", json={"url": "https://example.com/a"})
    assert renderer.budgets == [140, None]


def test_render_rejects_a_non_positive_budget() -> None:
    client, _ = _client(RenderResult(status="ok"))
    response = client.post("/render", json={"url": "https://example.com/a", "budget_seconds": 0})
    assert response.status_code == 422


def test_runtime_config_reports_environment_defaults(monkeypatch) -> None:
    monkeypatch.setenv("RENDER_ATTEMPTS", "6")
    monkeypatch.setenv("RENDER_GROW_WAIT_MS", "0")
    client, _ = _client(RenderResult(status="ok"))
    defaults = client.get("/runtime-config").json()["defaults"]
    assert defaults["RENDER_ATTEMPTS"] == 6
    assert defaults["RENDER_GROW_WAIT_MS"] == 0


def test_request_overrides_do_not_leak_to_next_request() -> None:
    client, renderer = _client(RenderResult(status="ok"))
    response = client.post(
        "/render",
        json={
            "url": "https://example.com/a",
            "RENDER_ATTEMPTS": 2,
            "RENDER_GROW_WAIT_MS": 0,
            "RENDER_BUDGET_SECONDS": 250,
        },
    )
    assert response.status_code == 200
    client.post("/render", json={"url": "https://example.com/b"})
    assert renderer.options == [RenderOptions(attempts=2, grow_wait_ms=0, budget_seconds=250), None]
    assert (
        client.post(
            "/render",
            json={
                "url": "https://example.com/a",
                "RENDER_ATTEMPTS": 0,
            },
        ).status_code
        == 422
    )


async def test_renderer_rejects_malformed_targets() -> None:
    renderer = CamoufoxRenderer()
    for url in ("https://[broken/a", "https://example.com:bad/a", "https://example.com:99999/a"):
        result = await renderer.render(url, True)
        assert result.status == "error"


def test_logging_updates_and_resets_without_replacing_handlers(monkeypatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("LOG_FORMAT", "json")
    client, _ = _client(RenderResult(status="ok"))
    root = logging.getLogger()
    handlers = list(root.handlers)
    old_level = root.level
    old_formatters = [handler.formatter for handler in handlers]
    try:
        response = client.put(
            "/runtime-config", json={"values": {"LOG_LEVEL": "DEBUG", "LOG_FORMAT": "text"}}
        )
        assert response.status_code == 200
        assert response.json()["effective"]["LOG_LEVEL"] == "DEBUG"
        assert root.level == logging.DEBUG
        assert root.handlers == handlers
        assert all(isinstance(handler.formatter, TextFormatter) for handler in handlers)
        reset = client.put("/runtime-config", json={"values": {}})
        assert reset.json()["effective"]["LOG_LEVEL"] == "INFO"
        assert root.level == logging.INFO
        assert (
            client.put("/runtime-config", json={"values": {"LOG_LEVEL": "invalid"}}).status_code
            == 422
        )
    finally:
        root.setLevel(old_level)
        for handler, formatter in zip(handlers, old_formatters, strict=True):
            handler.setFormatter(formatter)
