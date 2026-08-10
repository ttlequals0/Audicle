"""POST /api/v1/episodes/{id}/chapters -- regenerate chapters for a finished
episode from its stored transcript, without re-running the pipeline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.core import database
from app.core.paths import media_dir
from app.main import create_app
from app.services import episodes, transcript
from fastapi.testclient import TestClient


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def _seed(env: Path, *, id_: str = "ep1", duration: int = 1800, vtt: str | None = None) -> None:
    database.run_migrations(env)
    if vtt is None:
        vtt = transcript.build_vtt(
            [
                transcript.TranscriptChunk(text="Opening words.", duration_secs=600.0),
                transcript.TranscriptChunk(text="The middle part.", duration_secs=600.0),
                transcript.TranscriptChunk(text="The closing part.", duration_secs=600.0),
            ],
            silence_ms=0,
        )
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id=id_,
            job_id=None,
            original_url="https://example.test/a",
            title="An Article",
            author="Author",
            audio_path=f"/data/media/{id_}.mp3",
            artwork_path=None,
            transcript_vtt=vtt,
            duration_secs=duration,
        )
    finally:
        conn.close()


async def _fake_llm(system, user, _settings, **_kwargs):
    # The instructions must be in the user turn, ahead of the transcript: with
    # them only in the system prompt the model answers conversationally.
    assert "OUTPUT FORMAT" in user
    assert "[00:00]" in user
    assert user.index("OUTPUT FORMAT") < user.index("[00:00]")
    assert system  # a role line, never empty (some providers reject that)
    return "00:00 Opening remarks\n10:00 The middle\n20:00 Closing thoughts"


def test_regenerate_writes_chapters_and_serves_them(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import pipeline

    _seed(env)
    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake_llm)
    with _client(env) as client:
        response = client.post("/api/v1/episodes/ep1/chapters")
        assert response.status_code == 200, response.text
        assert response.json()["chapter_count"] == 3
        served = client.get("/media/ep1.chapters.json")
    assert served.status_code == 200
    doc = json.loads(served.content)
    assert doc["version"] == "1.2.0"
    assert doc["chapters"][0] == {"startTime": 0, "title": "Opening remarks"}
    assert doc["chapters"][2] == {"startTime": 1200, "title": "Closing thoughts"}


def test_regenerate_embeds_frames_when_the_mp3_exists(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings
    from app.services import pipeline

    _seed(env)
    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake_llm)
    embedded: dict = {}
    monkeypatch.setattr(
        pipeline.audio,
        "embed_chapters",
        lambda path, pairs, total: embedded.update(path=path, pairs=pairs, total=total),
    )
    media = media_dir(get_settings())
    media.mkdir(parents=True, exist_ok=True)
    (media / "ep1.mp3").write_bytes(b"FAKE_MP3")
    with _client(env) as client:
        assert client.post("/api/v1/episodes/ep1/chapters").status_code == 200
    assert embedded["pairs"][0] == (0.0, "Opening remarks")
    assert embedded["total"] == 1800


def test_regenerate_404_for_unknown_episode(env: Path) -> None:
    database.run_migrations(env)
    with _client(env) as client:
        assert client.post("/api/v1/episodes/nope/chapters").status_code == 404


def test_regenerate_409_without_a_transcript(env: Path) -> None:
    _seed(env, vtt="")
    with _client(env) as client:
        response = client.post("/api/v1/episodes/ep1/chapters")
    assert response.status_code == 409
    assert "transcript" in response.text.lower()


def test_regenerate_422_when_the_llm_returns_nothing_usable(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed generation must not wipe whatever chapters the episode had."""

    from app.services import pipeline

    _seed(env)

    async def _junk(_system, _user, _settings, **_kwargs):
        return "I could not find any chapters."

    monkeypatch.setattr(pipeline, "_llm_with_retry", _junk)
    with _client(env) as client:
        response = client.post("/api/v1/episodes/ep1/chapters")
    assert response.status_code == 422
    conn = database.connect(database.db_path(env))
    try:
        assert episodes.get_by_id(conn, "ep1").chapters_json is None
    finally:
        conn.close()


def test_regenerate_keeps_the_episode_guid_stable(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audio is unchanged, so subscribers must not be made to re-download it."""

    from app.services import pipeline

    _seed(env)
    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake_llm)
    conn = database.connect(database.db_path(env))
    try:
        before = episodes.get_by_id(conn, "ep1")
    finally:
        conn.close()
    with _client(env) as client:
        assert client.post("/api/v1/episodes/ep1/chapters").status_code == 200
    conn = database.connect(database.db_path(env))
    try:
        after = episodes.get_by_id(conn, "ep1")
    finally:
        conn.close()
    assert after.revision == before.revision
    assert after.updated_at == before.updated_at


def test_regenerate_uses_runtime_settings_not_just_env(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LLM connection lives in runtime_settings, not env, so the endpoint
    must apply the same overlay the worker does. Without it every call fails
    with "LLM base URL is not configured" even on a configured server."""

    from app.services import pipeline, runtime_settings

    _seed(env)
    conn = database.connect(database.db_path(env))
    try:
        runtime_settings.set_value(conn, "OPENAI_BASE_URL", "https://llm.example.test/v1")
    finally:
        conn.close()

    seen: dict = {}

    async def _capture(_system, _user, settings, **_kwargs):
        seen["base_url"] = settings.OPENAI_BASE_URL
        return "00:00 One\n10:00 Two"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _capture)
    with _client(env) as client:
        assert client.post("/api/v1/episodes/ep1/chapters").status_code == 200
    assert seen["base_url"] == "https://llm.example.test/v1"
