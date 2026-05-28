"""Job processing pipeline orchestrator.

Phase 2 wires only the extract stage. Subsequent phases append cleanup,
chunk, tts, audio, artwork, transcript, finalize.

Conventions enforced here:
- Every stage writes its name to ``jobs.stage`` BEFORE doing any work, so the
  job-timeout path can report exactly which stage was running.
- Stage start/end/failure emit structured log records with ``job_id`` +
  ``episode_id`` stamped via contextvars.
- The whole job runs under ``asyncio.wait_for(JOB_TIMEOUT_SECONDS)``; the
  timeout handler reads the last persisted stage and writes a clear error.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.config import Settings
from app.core import database
from app.services import corrections, extraction, jobs, llm
from app.services import prompt as prompt_service
from app.utils.logging import episode_id_ctx, job_id_ctx, stage_ctx

logger = logging.getLogger("app.services.pipeline")


@contextmanager
def _job_context(job: jobs.Job):
    tokens = (
        job_id_ctx.set(job.id),
        episode_id_ctx.set(job.episode_id),
    )
    try:
        yield
    finally:
        episode_id_ctx.reset(tokens[1])
        job_id_ctx.reset(tokens[0])


@contextmanager
def _stage_context(stage_name: str):
    token = stage_ctx.set(stage_name)
    try:
        yield
    finally:
        stage_ctx.reset(token)


async def process_job(job: jobs.Job, settings: Settings) -> None:
    """Run the configured pipeline stages against ``job``.

    Phase 2: extract only, then mark done. On any failure write
    ``stage`` + ``error`` and set status=failed. On timeout report the last
    persisted stage in the error message.
    """

    with _job_context(job):
        logger.info("Pipeline starting", extra={"event": "pipeline_start"})
        try:
            await asyncio.wait_for(
                _run_stages(job, settings),
                timeout=settings.JOB_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            _finalize_failure(
                job.id,
                settings,
                error_template=(
                    f"job exceeded JOB_TIMEOUT_SECONDS={settings.JOB_TIMEOUT_SECONDS} "
                    f"during stage {{stage}}"
                ),
                log_message="Pipeline timed out",
                log_event="pipeline_timeout",
                log_level=logging.WARNING,
                extra_log_fields={"timeout_seconds": settings.JOB_TIMEOUT_SECONDS},
            )
        except Exception as exc:
            _finalize_failure(
                job.id,
                settings,
                error_template=str(exc),
                log_message="Pipeline failed",
                log_event="pipeline_failed",
                log_level=logging.ERROR,
                with_exc_info=True,
            )
        else:
            logger.info("Pipeline finished", extra={"event": "pipeline_done"})


def _finalize_failure(
    job_id: str,
    settings: Settings,
    *,
    error_template: str,
    log_message: str,
    log_event: str,
    log_level: int,
    with_exc_info: bool = False,
    extra_log_fields: dict[str, Any] | None = None,
) -> None:
    """Shared exit path for both the timeout and generic-exception branches.

    Reads the last persisted stage (or "unknown"), writes the failure row, and
    emits the structured log line. Wraps the DB write in try/except so a
    secondary failure (locked DB, disk full) can't mask the original.
    """

    try:
        last_stage = _last_stage(job_id, settings) or "unknown"
    except Exception:
        last_stage = "unknown"
        logger.exception(
            "Failed to read last stage during error finalization",
            extra={"event": "finalize_read_failed"},
        )

    error_message = error_template.format(stage=last_stage)
    try:
        _persist_failure(job_id, stage=last_stage, error=error_message, settings=settings)
    except Exception:
        logger.exception(
            "Failed to persist job failure",
            extra={"event": "finalize_persist_failed", "stage": last_stage},
        )

    fields = {"event": log_event, "stage": last_stage}
    if extra_log_fields:
        fields.update(extra_log_fields)
    logger.log(log_level, log_message, extra=fields, exc_info=with_exc_info)


async def _run_stages(job: jobs.Job, settings: Settings) -> None:
    """Phase 3 pipeline: extract -> cleanup -> corrections.

    Phase 4+ appends chunk, tts, audio, artwork, transcript, finalize.
    """

    extraction_result = await _run_stage(
        "extract", lambda: _stage_extract(job, settings), job.id, settings
    )
    cleaned = await _run_stage(
        "cleanup",
        lambda: _stage_cleanup(extraction_result.markdown, settings),
        job.id,
        settings,
    )
    _ = await _run_stage(
        "corrections",
        lambda: _stage_corrections(cleaned, settings),
        job.id,
        settings,
    )
    _mark_done(job.id, final_stage="corrections", settings=settings)


async def _run_stage(
    name: str,
    body: Callable[[], Awaitable[Any]],
    job_id: str,
    settings: Settings,
) -> Any:
    """Run ``body`` as the named stage, persisting ``stage=name`` first and
    emitting stage_start / stage_end structured logs."""

    _set_stage(job_id, name, settings)
    with _stage_context(name):
        started = time.perf_counter()
        logger.info("Stage start", extra={"event": "stage_start"})
        try:
            result = await body()
        except BaseException:
            # BaseException covers asyncio.CancelledError (timeout) so the
            # stage_failed log fires for cancelled stages too.
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.exception(
                "Stage failed",
                extra={"event": "stage_failed", "duration_ms": duration_ms},
            )
            raise
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "Stage end",
            extra={"event": "stage_end", "duration_ms": duration_ms},
        )
        return result


async def _stage_extract(job: jobs.Job, settings: Settings) -> extraction.ExtractionResult:
    result = await extraction.extract(job.url, settings)
    logger.info(
        "Extraction succeeded",
        extra={
            "event": "extract_complete",
            "markdown_chars": len(result.markdown),
            "has_title": "title" in result.metadata,
        },
    )
    return result


class CleanupTooShortError(Exception):
    """LLM cleanup output came back below ``MIN_CLEANUP_CHARS``.

    Distinct from a generic ValueError so the pipeline's outer error handler
    can classify the failure as cleanup-specific and any future broad
    ``except ValueError`` doesn't accidentally swallow it.
    """


async def _stage_cleanup(markdown: str, settings: Settings) -> str:
    """LLM cleanup with tenacity retry on transient provider failures.

    Re-reads the prompt file every call so operator edits take effect on the
    next job without a restart. Per build plan (line 251), the cleanup stage
    wraps llm.generate with ``LLM_RETRY_COUNT`` attempts and exponential
    backoff, retrying only :class:`llm.LLMProviderError` / :class:`llm.LLMTimeoutError`.
    :class:`llm.LLMRequestError` (4xx, malformed response) is non-retryable.
    """

    prompt_path = _prompt_path(settings)
    system_prompt = prompt_service.load(prompt_path)
    cleaned = await _llm_with_retry(system_prompt, markdown, settings)
    if len(cleaned) < settings.MIN_CLEANUP_CHARS:
        raise CleanupTooShortError(
            f"Cleanup output is {len(cleaned)} chars, below "
            f"MIN_CLEANUP_CHARS={settings.MIN_CLEANUP_CHARS}"
        )
    logger.info(
        "Cleanup succeeded",
        extra={
            "event": "cleanup_complete",
            "input_chars": len(markdown),
            "output_chars": len(cleaned),
        },
    )
    return cleaned


async def _stage_corrections(cleaned: str, settings: Settings) -> str:
    """Apply the pronunciation dictionary. Re-reads the file every call."""

    dictionary_path = _corrections_path(settings)
    dictionary = corrections.load(dictionary_path)
    result = corrections.apply(cleaned, dictionary)
    logger.info(
        "Corrections applied",
        extra={
            "event": "corrections_complete",
            "entries_loaded": len(dictionary),
            "delta_chars": len(result) - len(cleaned),
        },
    )
    return result


async def _llm_with_retry(system: str, user: str, settings: Settings) -> str:
    """Call llm.generate with tenacity retry on transient errors only."""

    from tenacity import (
        AsyncRetrying,
        RetryError,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
    )

    retrying = AsyncRetrying(
        stop=stop_after_attempt(settings.LLM_RETRY_COUNT),
        wait=wait_exponential(multiplier=1, min=1),
        retry=retry_if_exception_type((llm.LLMProviderError, llm.LLMTimeoutError)),
        reraise=False,
    )
    try:
        async for attempt in retrying:
            with attempt:
                return await llm.generate(system, user, settings)
    except RetryError as exc:
        inner = exc.last_attempt.exception()
        if isinstance(inner, llm.LLMError):
            raise inner from exc
        raise llm.LLMProviderError(f"LLM retries exhausted: {inner}") from exc
    raise llm.LLMProviderError("LLM retry loop exited without a response")


def _prompt_path(_settings: Settings) -> Path:
    return Path(__file__).parent.parent / "prompts" / "script.txt"


def _corrections_path(_settings: Settings) -> Path:
    return Path(__file__).parent.parent / "corrections" / "pronunciation.json"


# --- DB helpers --------------------------------------------------------------
# All of these open and close their own connection. The worker process is the
# only writer in this pipeline, but each helper keeps its scope tight so a
# long-running stage doesn't hold a write lock.


def _set_stage(job_id: str, stage: str, settings: Settings) -> None:
    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        jobs.set_stage(conn, job_id, stage)
    finally:
        conn.close()


def _mark_done(job_id: str, *, final_stage: str, settings: Settings) -> None:
    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        jobs.mark_done(conn, job_id, final_stage=final_stage)
    finally:
        conn.close()


def _persist_failure(job_id: str, *, stage: str, error: str, settings: Settings) -> None:
    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        jobs.mark_failed(conn, job_id, stage=stage, error=error)
    finally:
        conn.close()


def _last_stage(job_id: str, settings: Settings) -> str | None:
    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        job = jobs.get_job(conn, job_id)
    finally:
        conn.close()
    return job.stage if job else None
