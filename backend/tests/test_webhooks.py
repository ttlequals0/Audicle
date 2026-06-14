from __future__ import annotations

import httpx
import pytest
from app.config import get_settings
from app.services import webhooks
from app.services.episodes import Episode
from app.services.jobs import Job


def _job(**kw) -> Job:
    base = dict(
        id="j1",
        url="https://example.test/article",
        episode_id="ep1",
        status="done",
        stage="finalize",
        error=None,
        created_at="2026-06-13T00:00:00Z",
        updated_at="2026-06-13T00:05:00Z",
        started_at="2026-06-13T00:00:30Z",
        reprocess=False,
    )
    base.update(kw)
    return Job(**base)


def _episode(**kw) -> Episode:
    base = dict(
        id="ep1",
        job_id="j1",
        title="An Article",
        author="A",
        original_url="https://example.test/article",
        audio_path="/m/ep1.mp3",
        artwork_path=None,
        transcript_vtt=None,
        duration_secs=60,
        pub_date="2026-06-13T00:05:00Z",
        created_at="2026-06-13T00:00:00Z",
        updated_at="2026-06-13T00:05:00Z",
    )
    base.update(kw)
    return Episode(**base)


def test_processed_payload_url() -> None:
    p = webhooks.build_payload("episode.processed", _job(reprocess=True), _episode())
    assert p["event"] == "episode.processed"
    assert p["title"] == "An Article"
    assert p["source_type"] == "url"
    assert p["url"] == "https://example.test/article"
    assert p["reprocess"] is True
    assert p["time_to_process_secs"] == 270.0
    assert "error" not in p


def test_processed_payload_upload_uses_filename() -> None:
    job = _job(url="upload://abc/My%20Doc.pdf", episode_id="up1")
    ep = _episode(id="up1", source_type="upload", source_filename="My Doc.pdf", title=None)
    p = webhooks.build_payload("episode.processed", job, ep)
    assert p["source_type"] == "upload"
    assert p["source_filename"] == "My Doc.pdf"
    assert p["title"] == "My Doc.pdf"  # falls back to filename when episode has no title


def test_failed_payload_has_error_and_stage() -> None:
    job = _job(status="failed", stage="tts", error="boom")
    p = webhooks.build_payload("episode.failed", job, None)
    assert p["event"] == "episode.failed"
    assert p["error"] == "boom"
    assert p["stage"] == "tts"
    assert p["title"] == "https://example.test/article"  # no episode -> url fallback


async def test_deliver_posts_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(__import__("json").loads(request.content))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: original(*a, **{**k, "transport": transport})
    )
    await webhooks._deliver("https://hook.test/x", {"event": "episode.processed"}, 5.0)
    assert sent == [{"event": "episode.processed"}]


async def test_deliver_swallows_dead_receiver(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: original(*a, **{**k, "transport": transport})
    )
    # Must not raise even though every attempt fails.
    await webhooks._deliver("https://hook.test/x", {"event": "episode.failed"}, 0.5, attempts=2)


def test_fire_is_noop_without_url(env) -> None:
    # No WEBHOOK_URL configured -> fire does nothing and doesn't need a loop.
    webhooks.fire(get_settings(), {"event": "episode.processed"})
