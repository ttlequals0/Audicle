"""Opportunistic idle-wrapper restart (0.56.0): the worker asks a memory-heavy
tts-wrapper to restart itself between jobs, when the queue is empty, instead
of only ever restarting mid-job at the wrapper's own hard limit."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import get_settings
from app.core import database
from app.services import jobs, runtime_settings, tts
from app.worker import _maybe_restart_idle_wrapper


async def test_restarts_when_queue_empty_and_restart_recommended(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)
    settings = get_settings()

    calls: list[str] = []

    async def _fake_health(_settings):
        return {"ok": True, "rss_mb": 9000, "restart_recommended": True}

    async def _fake_restart(_settings):
        calls.append("restart")

    monkeypatch.setattr(tts, "wrapper_health", _fake_health)
    monkeypatch.setattr(tts, "request_wrapper_restart", _fake_restart)

    await _maybe_restart_idle_wrapper(settings)

    assert calls == ["restart"]


async def test_does_not_restart_when_a_job_is_queued(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)
    settings = get_settings()

    conn = database.connect(database.db_path(env))
    try:
        jobs.create_job(conn, "https://example.test/queued-article")
    finally:
        conn.close()

    calls: list[str] = []

    async def _fake_health(_settings):
        # Would recommend a restart, but the queue-empty check must short
        # circuit before this is ever called.
        calls.append("health")
        return {"ok": True, "rss_mb": 9000, "restart_recommended": True}

    async def _fake_restart(_settings):
        calls.append("restart")

    monkeypatch.setattr(tts, "wrapper_health", _fake_health)
    monkeypatch.setattr(tts, "request_wrapper_restart", _fake_restart)

    await _maybe_restart_idle_wrapper(settings)

    assert calls == []


async def test_does_not_restart_when_restart_not_recommended(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)
    settings = get_settings()

    calls: list[str] = []

    async def _fake_health(_settings):
        return {"ok": True, "rss_mb": 100, "restart_recommended": False}

    async def _fake_restart(_settings):
        calls.append("restart")

    monkeypatch.setattr(tts, "wrapper_health", _fake_health)
    monkeypatch.setattr(tts, "request_wrapper_restart", _fake_restart)

    await _maybe_restart_idle_wrapper(settings)

    assert calls == []


async def test_does_nothing_when_setting_disabled(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)
    settings = get_settings().model_copy(update={"TTS_IDLE_RESTART_ENABLED": False})

    calls: list[str] = []

    async def _fake_health(_settings):
        calls.append("health")
        return {"ok": True, "rss_mb": 9000, "restart_recommended": True}

    monkeypatch.setattr(tts, "wrapper_health", _fake_health)

    await _maybe_restart_idle_wrapper(settings)

    assert calls == []


async def test_does_nothing_when_db_override_disables_setting(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TTS_IDLE_RESTART_ENABLED is in runtime_settings.ALLOWED_KEYS so an
    operator can flip it off from the Settings UI mid-incident with no worker
    restart. Passes the raw env settings (default TTS_IDLE_RESTART_ENABLED=
    True) so this only passes if _maybe_restart_idle_wrapper applies
    runtime_settings.overlay() itself, rather than trusting the caller to
    have already resolved it (the model_copy tests above stub that out)."""

    database.run_migrations(env)
    settings = get_settings()
    assert settings.TTS_IDLE_RESTART_ENABLED is True  # env default; DB overrides it below

    with database.connection(env) as conn:
        runtime_settings.set_value(conn, "TTS_IDLE_RESTART_ENABLED", False)

    calls: list[str] = []

    async def _fake_health(_settings):
        calls.append("health")
        return {"ok": True, "rss_mb": 9000, "restart_recommended": True}

    monkeypatch.setattr(tts, "wrapper_health", _fake_health)

    await _maybe_restart_idle_wrapper(settings)

    assert calls == []


async def test_does_nothing_for_the_openai_api_backend(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)
    settings = get_settings().model_copy(update={"TTS_BACKEND": "openai-api"})

    calls: list[str] = []

    async def _fake_health(_settings):
        calls.append("health")
        return {"ok": True, "rss_mb": 9000, "restart_recommended": True}

    monkeypatch.setattr(tts, "wrapper_health", _fake_health)

    await _maybe_restart_idle_wrapper(settings)

    assert calls == []


async def test_swallows_wrapper_health_failure(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opportunistic maintenance must never affect the worker loop: an
    unreachable wrapper or a transient error is just skipped."""

    database.run_migrations(env)
    settings = get_settings()

    async def _raise_health(_settings):
        raise tts.TTSUnreachableError("simulated: wrapper down")

    monkeypatch.setattr(tts, "wrapper_health", _raise_health)

    # Must not raise.
    await _maybe_restart_idle_wrapper(settings)


async def test_swallows_request_restart_failure(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 503 (inference busy, lost the race with the wrapper) must not
    propagate; the worker just tries again on its next idle window."""

    database.run_migrations(env)
    settings = get_settings()

    async def _fake_health(_settings):
        return {"ok": True, "rss_mb": 9000, "restart_recommended": True}

    async def _raise_restart(_settings):
        raise tts.TTSProviderError("simulated: inference busy")

    monkeypatch.setattr(tts, "wrapper_health", _fake_health)
    monkeypatch.setattr(tts, "request_wrapper_restart", _raise_restart)

    # Must not raise.
    await _maybe_restart_idle_wrapper(settings)
