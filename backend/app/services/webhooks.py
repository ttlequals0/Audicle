"""Fire-and-forget episode webhooks (0.31.0).

A bare POST to ``WEBHOOK_URL`` on every terminal job transition
(``episode.processed`` / ``episode.failed``). Delivery is scheduled as a
background task so a dead or slow receiver never delays or wedges the worker;
failures are logged, never raised into the pipeline. No HMAC signing this build.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import httpx

from app.config import Settings
from app.services import file_extraction
from app.services.episodes import Episode
from app.services.jobs import Job

logger = logging.getLogger("app.services.webhooks")

# Keep a strong reference to in-flight delivery tasks so they aren't GC'd mid-send.
_pending: set[asyncio.Task] = set()


def _source(job: Job, episode: Episode | None) -> dict[str, Any]:
    """The same url-vs-upload source branch the feed/UI use: a real link for url
    jobs, the filename for uploads (whose ``url`` is a synthetic ``upload://`` id)."""

    if file_extraction.is_upload_source(job.url):
        filename = (
            episode.source_filename
            if episode and episode.source_filename
            else file_extraction.parse_source_uri(job.url)[1]
        )
        return {"source_type": "upload", "source_filename": filename}
    return {"source_type": "url", "url": job.url}


def _time_to_process(job: Job) -> float | None:
    """Seconds from claim to terminal state; None when ``started_at`` is missing
    (pre-0.11.0 rows) or unparseable."""

    if not job.started_at:
        return None
    try:
        start = datetime.fromisoformat(job.started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(job.updated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    secs = (end - start).total_seconds()
    return secs if secs >= 0 else None


def build_payload(
    event: str, job: Job, episode: Episode | None, *, voice_label: str | None = None
) -> dict[str, Any]:
    """Assemble the webhook body for ``episode.processed`` / ``episode.failed``.

    ``voice_label`` is the reference voice that narrated (or, on failure, would
    have narrated) -- the caller resolves it from the episode snapshot or the
    job's slot."""

    source = _source(job, episode)
    title = (
        (episode.title if episode and episode.title else None)
        or source.get("source_filename")
        or source.get("url")
    )
    payload: dict[str, Any] = {
        "event": event,
        "episode_id": job.episode_id,
        "title": title,
        "voice": voice_label,
        "reprocess": job.reprocess,
        **source,
    }
    if event == "episode.failed":
        payload["error"] = job.error
        payload["stage"] = job.stage
    else:
        payload["time_to_process_secs"] = _time_to_process(job)
    return payload


def fire(settings: Settings, payload: dict[str, Any]) -> None:
    """Schedule delivery of ``payload`` to ``WEBHOOK_URL`` and return immediately.
    No-op when the webhook is unconfigured."""

    url = settings.WEBHOOK_URL.strip()
    if not url:
        return
    try:
        task = asyncio.ensure_future(_deliver(url, payload, settings.WEBHOOK_TIMEOUT_SECONDS))
    except RuntimeError:
        # No running loop (shouldn't happen from the async worker); skip rather than crash.
        logger.warning("No event loop for webhook delivery", extra={"event": "webhook_no_loop"})
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _deliver(url: str, payload: dict[str, Any], timeout: float, *, attempts: int = 3) -> None:
    """POST with a short timeout and a few retries. Never raises."""

    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code < 500:
                if resp.status_code >= 400:
                    logger.warning(
                        "Webhook receiver returned client error",
                        extra={"event": "webhook_client_error", "status": resp.status_code},
                    )
                return
        except httpx.HTTPError as exc:
            if attempt == attempts - 1:
                logger.warning(
                    "Webhook delivery failed after retries",
                    extra={"event": "webhook_failed", "error_class": type(exc).__name__},
                )
                return
        await asyncio.sleep(0.5 * (2**attempt))


def sample_payload() -> dict[str, Any]:
    """A representative ``episode.processed`` body (flagged ``test``) for the
    'send test webhook' control, so an operator can wire up a receiver before a
    real episode runs."""

    return {
        "event": "episode.processed",
        "episode_id": "test00000000",
        "title": "Test webhook from Audicle",
        "voice": "Default",
        "reprocess": False,
        "source_type": "url",
        "url": "https://example.com/article",
        "time_to_process_secs": 42.0,
        "test": True,
    }


async def send_test(settings: Settings) -> dict[str, Any]:
    """POST the sample payload to ``WEBHOOK_URL`` once (no retries) and report the
    outcome, so the Settings UI / API can show whether the receiver accepted it.
    Unlike the fire-and-forget pipeline path, this returns the exact result and
    never raises."""

    url = settings.WEBHOOK_URL.strip()
    if not url:
        return {"delivered": False, "status_code": None, "error": "no webhook URL configured"}
    try:
        async with httpx.AsyncClient(timeout=settings.WEBHOOK_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=sample_payload())
    except httpx.HTTPError as exc:
        return {"delivered": False, "status_code": None, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "delivered": resp.is_success,
        "status_code": resp.status_code,
        "error": None if resp.is_success else (resp.text[:200] or f"HTTP {resp.status_code}"),
    }
