"""Job processing pipeline orchestrator.

The full chain: extract -> cleanup -> normalize -> summary -> chunk ->
tts -> audio -> artwork -> transcript -> finalize. Finalize upserts the
``episodes`` row that the RSS feed and ``/media/{id}.{mp3,jpg,vtt}``
handlers serve.

Conventions enforced here:
- Every stage writes its name to ``jobs.stage`` BEFORE doing any work, so the
  job-timeout path can report exactly which stage was running.
- Stage start/end/failure emit structured log records with ``job_id`` +
  ``episode_id`` stamped via contextvars.
- The whole job runs under an ``asyncio.timeout`` whose deadline starts at
  ``JOB_TIMEOUT_SECONDS`` and is rescheduled once the chunk count is known to
  ``max(JOB_TIMEOUT_SECONDS, chunks * JOB_TIMEOUT_PER_CHUNK_SECONDS)`` -- a long
  document gets proportionally more time. The timeout handler reads the last
  persisted stage and writes a clear error.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.config import Settings
from app.core import database
from app.core.paths import file_size_or_zero, media_dir
from app.services import (
    article_prep,
    artwork,
    asr_verify,
    audio,
    audio_analysis,
    chunker,
    cleanup_output,
    corrections,
    episodes,
    extraction,
    file_extraction,
    jobs,
    lexicon,
    llm,
    pronounce_convert,
    source_fallbacks_store,
    transcript,
    tts,
    voices,
    webhooks,
)
from app.services import prompt as prompt_service
from app.utils.logging import episode_id_ctx, job_id_ctx, stage_ctx

logger = logging.getLogger("app.services.pipeline")

# Upper bound on the cleaned-article text fed to the show-notes summary call.
# A topic-level blurb doesn't need the whole body; this bounds the input tokens
# while leaving typical articles (under the cap) summarized in full.
_SUMMARY_MAX_INPUT_CHARS = 16000


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


def effective_job_timeout(settings: Settings, chunk_count: int) -> float:
    """Per-job timeout in seconds: the base ceiling, or the chunk-scaled budget
    when the document is large enough to need it."""

    return max(
        settings.JOB_TIMEOUT_SECONDS,
        chunk_count * settings.JOB_TIMEOUT_PER_CHUNK_SECONDS,
    )


class JobCancelledError(Exception):
    """Raised mid-run when the operator flips a job's status to 'cancelled'. The worker
    catches it and leaves the row cancelled instead of finalizing a failure."""


def _load_job(job_id: str, settings: Settings) -> jobs.Job | None:
    """Read one job row on its own short-lived connection."""

    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        return jobs.get_job(conn, job_id)
    finally:
        conn.close()


def _raise_if_cancelled(job_id: str, settings: Settings) -> None:
    """Raise JobCancelledError if the operator cancelled this job. Best-effort: any read
    error is swallowed so a transient DB hiccup never aborts a running job."""

    try:
        job = _load_job(job_id, settings)
    except Exception:
        return
    if job is not None and job.status == "cancelled":
        raise JobCancelledError


async def process_job(job: jobs.Job, settings: Settings) -> None:
    """Run the configured pipeline stages against ``job``.

    On any failure write ``stage`` + ``error`` and set status=failed. On
    timeout report the last persisted stage in the error message.

    The job runs under an ``asyncio.timeout`` that starts at
    ``JOB_TIMEOUT_SECONDS`` and is rescheduled once the chunk count is known so a
    long document gets ``max(base, chunks * JOB_TIMEOUT_PER_CHUNK_SECONDS)``.
    """

    base = settings.JOB_TIMEOUT_SECONDS
    # Mutable cell the reschedule hook updates so the timeout error message can
    # report the effective deadline + chunk count, not just the base.
    timeout_state = {"effective": base, "chunks": 0}

    with _job_context(job):
        logger.info("Pipeline starting", extra={"event": "pipeline_start"})
        terminal_event = "episode.processed"
        try:
            loop = asyncio.get_running_loop()
            t0 = loop.time()

            def _rescale(chunk_count: int) -> None:
                eff = effective_job_timeout(settings, chunk_count)
                timeout_state["effective"] = eff
                timeout_state["chunks"] = chunk_count
                if eff > base:
                    # Deadline is absolute (loop clock) from job start.
                    cm.reschedule(t0 + eff)
                    logger.info(
                        "Job timeout rescaled for chunk count",
                        extra={
                            "event": "job_timeout_rescaled",
                            "chunk_count": chunk_count,
                            "timeout_seconds": eff,
                            "base_timeout": base,
                        },
                    )

            async with asyncio.timeout(base) as cm:
                await _run_stages(job, settings, on_chunk_count=_rescale)
        except JobCancelledError:
            # Operator cancelled mid-run; jobs.mark_cancelled already set the status.
            # Don't finalize a failure and don't fire a terminal webhook -- just stop.
            logger.info("Pipeline cancelled", extra={"event": "pipeline_cancelled"})
            return
        except TimeoutError:
            terminal_event = "episode.failed"
            eff = timeout_state["effective"]
            chunks = timeout_state["chunks"]
            _finalize_failure(
                job.id,
                settings,
                error_template=(
                    f"job exceeded its timeout of {eff:.0f}s "
                    f"(JOB_TIMEOUT_SECONDS={base}, {chunks} chunks) during stage {{stage}}"
                ),
                log_message="Pipeline timed out",
                log_event="pipeline_timeout",
                log_level=logging.WARNING,
                extra_log_fields={
                    "timeout_seconds": eff,
                    "base_timeout": base,
                    "chunk_count": chunks,
                },
            )
        except Exception as exc:
            terminal_event = "episode.failed"
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
        # Fire-and-forget terminal webhook (no-op when WEBHOOK_URL is unset).
        _fire_webhook(terminal_event, job.id, settings)


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

    # Use ``str.replace`` instead of ``str.format`` so user-controlled
    # exception text (ffmpeg stderr, JSON bodies, sentence previews) that
    # contains literal ``{`` or ``}`` doesn't trigger a secondary KeyError
    # inside the failure handler and leave the job stuck in 'processing'.
    error_message = error_template.replace("{stage}", last_stage)
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


def _fire_webhook(event: str, job_id: str, settings: Settings) -> None:
    """Build + schedule the terminal episode webhook. Best-effort: a webhook problem
    never propagates into job processing. No-op when ``WEBHOOK_URL`` is unset."""

    if not settings.WEBHOOK_URL.strip():
        return
    try:
        conn = database.connect(database.db_path(settings.DATA_DIR))
        try:
            job = jobs.get_job(conn, job_id)
            episode = episodes.get_by_id(conn, job.episode_id) if job else None
            voice_label = None
            if job is not None:
                # Prefer the episode's voice snapshot; on failure (no episode)
                # resolve the voice the job would have used from its slot.
                voice_label = (
                    episode.voice_label if episode and episode.voice_label else None
                ) or voices.label_for(conn, job.voice_id)
        finally:
            conn.close()
        if job is not None:
            webhooks.fire(
                settings, webhooks.build_payload(event, job, episode, voice_label=voice_label)
            )
    except Exception:
        logger.warning(
            "Failed to build episode webhook",
            extra={"event": "webhook_build_failed"},
            exc_info=True,
        )


async def _run_stages(
    job: jobs.Job,
    settings: Settings,
    *,
    on_chunk_count: Callable[[int], None] | None = None,
) -> None:
    """Run the stages in order: extract -> cleanup -> normalize -> summary ->
    chunk -> tts -> audio -> artwork -> transcript -> finalize (finalize
    inserts/updates the episodes row).

    ``on_chunk_count`` is invoked with the chunk count right after chunking so
    the caller can rescale the job timeout before the (longest) TTS stage runs."""

    extraction_result = await _run_stage(
        "extract", lambda: _stage_extract(job, settings), job.id, settings
    )
    cleaned = await _run_stage(
        "cleanup",
        lambda: _stage_cleanup(job.id, extraction_result.markdown, settings),
        job.id,
        settings,
    )
    normalized = await _run_stage(
        "normalize",
        lambda: _stage_normalize(job, cleaned, settings),
        job.id,
        settings,
    )
    summary = await _run_stage(
        "summary",
        lambda: _stage_summary(normalized, settings),
        job.id,
        settings,
    )
    chunks = await _run_stage(
        "chunk",
        lambda: _stage_chunk(normalized, settings),
        job.id,
        settings,
    )
    # Rescale the job timeout now that the workload (chunk count) is known, before
    # the TTS stage -- the longest stage -- begins.
    if on_chunk_count is not None:
        on_chunk_count(len(chunks))
    chunk_results = await _run_stage(
        "tts",
        lambda: _stage_tts(job, chunks, settings),
        job.id,
        settings,
    )
    audio_result = await _run_stage(
        "audio",
        lambda: _stage_audio(job, chunk_results, settings),
        job.id,
        settings,
    )
    artwork_result = await _run_stage(
        "artwork",
        lambda: _stage_artwork(job, extraction_result.metadata, settings),
        job.id,
        settings,
    )
    vtt = await _run_stage(
        "transcript",
        lambda: _stage_transcript(chunks, chunk_results, settings),
        job.id,
        settings,
    )
    await _run_stage(
        "finalize",
        lambda: _stage_finalize(
            job,
            metadata=extraction_result.metadata,
            audio_result=audio_result,
            artwork_result=artwork_result,
            vtt=vtt,
            summary=summary,
            cleaned_text=normalized,
            settings=settings,
        ),
        job.id,
        settings,
    )
    _mark_done(job.id, final_stage="finalize", settings=settings)


async def _run_stage(
    name: str,
    body: Callable[[], Awaitable[Any]],
    job_id: str,
    settings: Settings,
) -> Any:
    """Run ``body`` as the named stage, persisting ``stage=name`` first and
    emitting stage_start / stage_end structured logs."""

    _set_stage(job_id, name, settings)
    # Cancel checkpoint at every stage boundary: an operator cancel between stages aborts
    # here before the stage runs, without logging it as a stage failure.
    _raise_if_cancelled(job_id, settings)
    with _stage_context(name):
        started = time.perf_counter()
        logger.info("Stage start", extra={"event": "stage_start"})
        try:
            result = await body()
        except JobCancelledError:
            raise  # an operator cancel mid-stage is not a stage failure
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
    # Uploaded documents carry a synthetic ``upload://`` source: read and parse the
    # stored file instead of fetching a URL. Everything downstream is identical.
    if file_extraction.is_upload_source(job.url):
        return await file_extraction.extract_file(job, settings)
    # Build the effective paywall-fallback registry (operator rules over built-ins, plus
    # a global-default catch-all) so extraction routes known paywall hosts through the
    # configured bypass. Shared with the /source-fallbacks/test endpoint.
    registry = source_fallbacks_store.load_registry(settings)
    result = await extraction.extract(job.url, settings, registry)
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


# Residual markdown ATX heading markers (``### ``) the LLM occasionally leaves
# despite the plain-text prompt; the TTS would otherwise read the hashes aloud.
_HEADING_MARKER_RE = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]+")


def _strip_heading_markers(text: str) -> str:
    """Drop leading markdown heading hashes, keeping the heading text."""

    return _HEADING_MARKER_RE.sub("", text)


# Month abbreviations expanded only in date context (followed by a day/year
# number) so plain "Jan"/"Mar"/"Aug" used as names are left untouched. February
# resolves to its corrected pronunciation via the corrections dictionary.
_DATE_MONTHS = {
    "Jan": "January",
    "Feb": "February",
    "Mar": "March",
    "Apr": "April",
    "Jun": "June",
    "Jul": "July",
    "Aug": "August",
    "Sept": "September",
    "Sep": "September",
    "Oct": "October",
    "Nov": "November",
    "Dec": "December",
}
# Longest alternatives first so "Sept" wins over "Sep"; optional trailing period
# is consumed; lookahead requires whitespace then a digit (the date's day/year).
_DATE_MONTH_RE = re.compile(
    r"\b(" + "|".join(sorted(_DATE_MONTHS, key=len, reverse=True)) + r")\.?(?=\s+\d)"
)


def _normalize_date_months(text: str) -> str:
    """Expand a month abbreviation to its full name when it heads a date.

    "Jan 15 2026" -> "January 15 2026"; "Feb. 3" -> "February 3". A bare
    abbreviation not followed by a number (often a name) is left as-is.
    """

    return _DATE_MONTH_RE.sub(lambda m: _DATE_MONTHS[m.group(1)], text)


# Grouped thousands (1,234 / 1,234,567) and decimals (3.14, 1,234.56): two number
# shapes the engine reliably garbles. One token must carry a comma group or a
# decimal point to match -- bare integers (15, 2026), unit-attached numbers
# (500m), versions (1.2.3), and code-glued digits (x86, startup_32) are
# deliberately left to the LLM prompt because they are context-dependent (a year
# reads "twenty twenty-six", an emergency number reads "nine one one"). The
# (?<![\w.,]) lookbehind keeps this off identifiers and the middle of a dotted
# version string or IP address. The trailing (?!\.\d|[,\w]) rejects only a
# number glued to a following digit via "." or "," or to a word char (so
# "1.2.3" and "x86" stay untouched) while still matching a number that ends a
# sentence ("the cost was 1,234.56." -> spelled).
_SPELLABLE_NUMBER_RE = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+)(?!\.\d|[,\w])"
)

# Money with a currency symbol and an optional magnitude suffix (k/m/b/t). The
# leading symbol is what disambiguates the suffix from a unit -- "$500m" is
# "five hundred million dollars" while a bare "500m north" stays meters (left to
# the LLM prompt). Trailing (?!\w) keeps "$500kg" out (k then g) so a mis-suffixed
# token falls through untouched rather than being read wrong.
# The magnitude may be glued as a single letter ("$3M") or written as a word
# after a space ("$3 million"). The word branch needs (?!\w) so "$3 millionaire"
# expands only the "$3" and leaves the noun alone.
_CURRENCY_RE = re.compile(
    r"(?<!\w)([$€£])\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)"
    r"(?:([kKmMbBtT])|\s+((?i:thousand|million|billion|trillion)))?(?!\w)"
)
_CURRENCY_WORDS = {"$": "dollars", "€": "euros", "£": "pounds"}
_MAGNITUDE_WORDS = {"k": "thousand", "m": "million", "b": "billion", "t": "trillion"}


# Self-contained integer-to-words (num2words is LGPL, outside the project's
# license allow-list). Covers up to quintillions; beyond the scale table we fall
# back to digit-by-digit, which is never wrong.
_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_SCALES = ("", "thousand", "million", "billion", "trillion", "quadrillion", "quintillion")


def _three_to_words(n: int) -> list[str]:
    """Words for 0..999 (empty list for 0, so callers can skip empty groups)."""

    words: list[str] = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        words += [_ONES[hundreds], "hundred"]
    if rest < 20:
        if rest:
            words.append(_ONES[rest])
    else:
        tens, ones = divmod(rest, 10)
        words.append(_TENS[tens] if not ones else f"{_TENS[tens]}-{_ONES[ones]}")
    return words


def _int_to_words(n: int) -> str | None:
    """Cardinal spelling of a non-negative int, or None if past the scale table."""

    if n == 0:
        return "zero"
    groups: list[int] = []
    while n > 0:
        n, rem = divmod(n, 1000)
        groups.append(rem)
    if len(groups) > len(_SCALES):
        return None
    words: list[str] = []
    for i in range(len(groups) - 1, -1, -1):
        if groups[i]:
            words += _three_to_words(groups[i])
            if i:
                words.append(_SCALES[i])
    return " ".join(words)


def _digits_to_words(digits: str) -> str:
    return " ".join(_ONES[int(d)] for d in digits)


def _spell_number(token: str) -> str:
    """Spell one grouped/decimal number. Fractions read digit-by-digit so
    trailing zeros survive ("2.0" -> "two point zero", not "two")."""

    if "." in token:
        integer, fraction = token.replace(",", "").split(".")
        whole = _int_to_words(int(integer)) or _digits_to_words(integer)
        return f"{whole} point {_digits_to_words(fraction)}"
    integer = token.replace(",", "")
    return _int_to_words(int(integer)) or _digits_to_words(integer)


def _normalize_currency(text: str) -> str:
    """Expand currency amounts, including magnitude suffixes, into spoken words.

    "$500k" -> "five hundred thousand dollars", "$3.5M" -> "three point five
    million dollars", "$1,200" -> "one thousand two hundred dollars". Reuses
    ``_spell_number`` for the numeric part. Runs before ``_normalize_numbers`` so
    the digits are already words by the time the bare-number pass looks at them.
    """

    def repl(match: re.Match[str]) -> str:
        symbol, number = match.group(1), match.group(2)
        mag_letter, mag_word = match.group(3), match.group(4)
        words = _spell_number(number)
        if mag_letter:
            words = f"{words} {_MAGNITUDE_WORDS[mag_letter.lower()]}"
        elif mag_word:
            words = f"{words} {mag_word.lower()}"
        return f"{words} {_CURRENCY_WORDS[symbol]}"

    return _CURRENCY_RE.sub(repl, text)


def _normalize_numbers(text: str) -> str:
    """Spell grouped-thousand and decimal numbers the LLM left as digits.

    "1,234,567" -> "one million two hundred thirty-four thousand five hundred
    sixty-seven"; "3.14" -> "three point one four". Narrow on purpose: see the
    regex comment for why bare integers and code-glued digits are excluded.
    """

    return _SPELLABLE_NUMBER_RE.sub(lambda m: _spell_number(m.group(1)), text)


# snake_case / __dunder code identifiers the LLM/corrections didn't rewrite, read
# as spaced words so the engine doesn't hallucinate on the underscores. Dotted file
# tokens (node.js, head64.c) are deliberately left to the LLM cleanup rule
# ("file or command names become plain spoken language") and explicit seed rows
# -- a generic word.ext transform here mis-speaks framework names and TLDs.
_SNAKE_IDENTIFIER_RE = re.compile(r"(?<![\w-])_*([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)(?![\w-])")


def _normalize_identifiers(text: str) -> str:
    """Expand leftover snake_case code identifiers into spaced spoken words.

    "startup_32" -> "startup 32", "__startup_64" -> "startup 64",
    "reset_early_page_tables" -> "reset early page tables". Runs after the
    corrections dictionary so explicit pronunciations (e.g. "ttyS0" -> "T T Y S
    0") win first and this only mops up identifiers no rule covered.
    """

    return _SNAKE_IDENTIFIER_RE.sub(lambda m: m.group(1).replace("_", " "), text)


# Inline-code leftovers the LLM keeps on code-dense articles despite the prompt:
# backtick fences, empty call-parens after a name ("smp init()"), and hex
# literals. A hex address read digit-by-digit is unintelligible noise, so it is
# replaced with a short spoken phrase rather than spelled out.
_HEX_LITERAL_RE = re.compile(r"\b0x[0-9A-Fa-f]+\b")
_EMPTY_CALL_PARENS_RE = re.compile(r"(?<=\w)\(\)")


def _strip_code_artifacts(text: str) -> str:
    """Remove inline-code noise the engine can't voice: backticks, ``()`` call
    syntax, and hex literals (replaced with a spoken phrase)."""

    text = text.replace("`", "")
    text = _HEX_LITERAL_RE.sub("a hexadecimal value", text)
    return _EMPTY_CALL_PARENS_RE.sub("", text)


# A numeric range written with a dash reads as "to" ("2017-2021" -> "2017 to
# 2021", "pages 10-12" -> "pages 10 to 12"). Matches a run of two-or-more numbers
# joined by hyphen/en-dash/em-dash; the replacement only fires for an exact PAIR
# so ISO dates ("2024-01-15") and dashed phone chains ("1-800-555-1234") -- three
# or more numbers -- are left untouched. Word boundaries keep it off hyphenated
# words (no digits) and code identifiers. Years the LLM already spelled to words
# are handled by the cleanup prompt, not here; this is the digit-form backstop.
_RANGE_DASH = "\\s*[-\u2013\u2014]\\s*"  # hyphen, en dash (U+2013), em dash (U+2014)
_NUM = r"\d{1,4}(?:,\d{3})*(?:\.\d+)?"
_RANGE_CHAIN_RE = re.compile(rf"(?<![\w.])({_NUM}(?:{_RANGE_DASH}{_NUM})+)(?![\w.])")
_RANGE_SPLIT_RE = re.compile(_RANGE_DASH)


def _normalize_ranges(text: str) -> str:
    """Replace a numeric ``A-B`` range with ``A to B`` (digit forms only).

    Only a two-number pair is treated as a range; longer dash-number chains
    (dates, phone numbers) are left unchanged.
    """

    def repl(match: re.Match[str]) -> str:
        parts = _RANGE_SPLIT_RE.split(match.group(1))
        if len(parts) != 2:
            return match.group(0)
        return f"{parts[0]} to {parts[1]}"

    return _RANGE_CHAIN_RE.sub(repl, text)


# Dotted acronyms ("A.I.", "U.S.", "U.S.A.") -- the engine reads each period as a
# pause ("A <pause> I"), so collapse the dots to spaced letters it voices cleanly.
# Uppercase-only (so "e.g."/"i.e." and decimals are untouched); needs 2+ letter-dot
# pairs so a lone "A." (sentence) doesn't match.
_DOTTED_ACRONYM_RE = re.compile(r"(?:[A-Z]\.){2,}")


def _normalize_dotted_acronyms(text: str) -> str:
    """Turn "A.I." into "A I" so the engine doesn't pause on the periods."""

    return _DOTTED_ACRONYM_RE.sub(
        lambda m: " ".join(ch for ch in m.group(0) if ch.isalpha()), text
    )


# Two-letter US state codes -> full names, expanded only in clear state context
# so the engine says "Illinois" instead of spelling "I L".
_US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}
# Codes whose UPPERCASE form is a common non-state word/abbreviation in prose
# (OK=okay, OR=or, ID=identification, AR=augmented reality, VA=Veterans Affairs,
# PA=public address, MA=Master of Arts, MS=Master of Science/Ms., LA=Los Angeles).
# These never take the city-comma form; they expand only inside a "City, ST ZIP"
# address, where the ZIP removes the ambiguity. (Codes that collide only in their
# rarely-uppercased forms -- "in", "oh", "de facto" -- are left in the city form,
# since real prose writes those lowercase.)
_STATE_AMBIGUOUS = {"OK", "OR", "ID", "AR", "VA", "PA", "MA", "MS", "LA"}
# City context: a Title-case city word ends in a lowercase letter, so requiring a
# lowercase letter immediately before the comma excludes all-caps tokens (SQL,
# acronyms, "JOIN, NY"). The comma+space is captured and re-emitted; trailing
# sentence punctuation is left in place (lookahead, not consumed). Ambiguous codes
# are excluded here.
_US_STATE_CITY_RE = re.compile(
    r"(?<=[a-z])(,\s+)("
    + "|".join(c for c in _US_STATES if c not in _STATE_AMBIGUOUS)
    + r")(?=[\s.,;:!?)\]]|$)"
)
# Address context: "..., ST 60601" -- a comma-anchored state code on the same line
# directly before a 5-digit ZIP (+4 optional). The comma anchor keeps a bare
# "<CODE> 12345" quantity from expanding; same-line ([ \t], not \s) avoids merging
# across a line break. Every code (including the ambiguous ones) expands here.
_US_STATE_ZIP_RE = re.compile(
    r"(,\s+)(" + "|".join(_US_STATES) + r")[ \t]+(?=\d{5}(?:-\d{4})?\b)"
)


def _normalize_us_states(text: str) -> str:
    """Expand a two-letter US state code to its full name in state context only:
    after a city + comma ("Chicago, IL" -> "Chicago, Illinois") or inside a
    "City, ST ZIP" address ("Tulsa, OK 74103" -> "Tulsa, Oklahoma 74103"). The
    lowercase-before-comma guard, case-sensitive uppercase codes, and the
    ambiguous-code set leave ordinary words/abbreviations (OK, OR, Co., Mt., a PA
    system, an MA degree) untouched outside a ZIP address. Sentence punctuation
    after the code is preserved ("Chicago, IL." -> "Chicago, Illinois.")."""

    text = _US_STATE_CITY_RE.sub(lambda m: m.group(1) + _US_STATES[m.group(2)], text)
    return _US_STATE_ZIP_RE.sub(lambda m: m.group(1) + _US_STATES[m.group(2)] + " ", text)


def _normalize_for_tts(text: str) -> str:
    """Deterministic fixups for things the cleanup prompt doesn't reliably catch.

    One ordered pass so future rules have a single home: strip residual markdown
    heading markers, strip inline-code artifacts (backticks, call-parens, hex),
    expand date-context month abbreviations, turn numeric dash-ranges into "to",
    then spell the number shapes the engine garbles. Runs at the end of cleanup, before
    the pronunciation dictionary, so e.g. "Feb 3" becomes "February 3" and the
    corrections dict can then voice it correctly. Code-artifact stripping runs
    before number spelling so a hex literal is gone before the number pass ever
    looks at it; range expansion runs before number spelling so "1,000-2,000"
    becomes "1,000 to 2,000" and both grouped numbers are then spelled.
    """

    return _normalize_numbers(
        _normalize_currency(
            _normalize_ranges(
                _normalize_date_months(
                    _normalize_us_states(
                        _strip_code_artifacts(_strip_heading_markers(text))
                    )
                )
            )
        )
    )


async def _stage_cleanup(job_id: str, markdown: str, settings: Settings) -> str:
    """LLM cleanup with tenacity retry on transient provider failures.

    Re-reads the prompt file every call so operator edits take effect on the
    next job without a restart. Per build plan (line 251), the cleanup stage
    wraps llm.generate with ``LLM_RETRY_COUNT`` attempts and exponential
    backoff, retrying only :class:`llm.LLMProviderError` / :class:`llm.LLMTimeoutError`.
    :class:`llm.LLMRequestError` (4xx, malformed response) is non-retryable.
    """

    with database.connection(settings.DATA_DIR) as conn:
        system_prompt = prompt_service.load_effective(conn, "cleanup")

    # Pronunciation respelling is no longer done here -- it moved to the dedicated
    # normalize stage (LLM pronunciation pass + deterministic backstop) so cleanup
    # stays focused on de-chroming the scrape.

    # Strip wiki-style chrome (TOC, [edit] markers, citation superscripts, and
    # trailing See also/References/External links link dumps) before windowing so
    # each window is article-shaped rather than dominated by page furniture.
    markdown = article_prep.strip_chrome(markdown)

    # Process the article in paragraph-bounded windows, one LLM call each, then
    # concatenate. A single giant call capped the output at LLM_MAX_TOKENS and
    # truncated long articles to the first paragraph; windowing keeps each call's
    # output well under the cap so article length is never the bottleneck.
    windows = chunker.pack_paragraphs(markdown, settings.LLM_CLEANUP_WINDOW_CHARS) or [markdown]
    cleaned_parts: list[str] = []
    for index, window in enumerate(windows):
        # Repeat the directive in the user turn (many models weight it higher
        # than the system prompt) and delimit the article so the model cleans it
        # rather than replying conversationally to it. The marker contract lets
        # the parser drop any preamble the model glues on top of the narration.
        user_message = (
            "Clean the article below per your instructions. Output ONLY the "
            f"cleaned narration text between a line {cleanup_output.BEGIN_MARKER} "
            f"and a line {cleanup_output.END_MARKER} -- no commentary, greetings, "
            "or questions outside the markers. If this passage has no article "
            "body, output exactly NO_ARTICLE_CONTENT and nothing else."
            f"\n\n<article>\n{window}\n</article>"
        )
        raw = await _llm_with_retry(system_prompt, user_message, settings)
        part = cleanup_output.extract_clean_output(raw)
        if cleanup_output.needs_compliance_retry(raw, part):
            raw = await _llm_with_retry(
                system_prompt, cleanup_output.RETRY_INSTRUCTION + user_message, settings
            )
            part = cleanup_output.extract_clean_output(raw)
        # Drop boilerplate-only windows and any refusal the model still leaked, so
        # a "there is no article" line never reaches narration; the empty string
        # is filtered out of the join below.
        empty = (
            not part
            or cleanup_output.is_empty_section(part)
            or cleanup_output.is_refusal_output(part)
        )
        output_chars = len(part)  # the model's real output, before we drop it
        if empty:
            part = ""
        cleaned_parts.append(part)
        _set_progress(job_id, index + 1, len(windows), settings)
        logger.info(
            "Cleanup window done",
            extra={
                "event": "cleanup_window_empty" if empty else "cleanup_window_done",
                "window_index": index,
                "window_count": len(windows),
                "input_chars": len(window),
                "output_chars": output_chars,
            },
        )
    # Deterministic _normalize_for_tts runs later in the normalize stage, once on
    # the full text, so cleanup just joins the surviving windows here.
    cleaned = "\n\n".join(p for p in cleaned_parts if p)
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
            "window_count": len(windows),
        },
    )
    return cleaned


async def _stage_chunk(corrected: str, settings: Settings) -> list[str]:
    """Hybrid chunking. Raises :class:`chunker.UnsplittableSentenceError` so
    the pipeline marks the job failed with a clear preview of the offending
    sentence rather than silently truncating content."""

    pieces = chunker.chunk(corrected, settings)
    if not pieces:
        raise ValueError("Chunker produced zero chunks from corrected text")
    word_counts = [len(p.split()) for p in pieces]
    logger.info(
        "Chunking complete",
        extra={
            "event": "chunk_complete",
            "chunk_count": len(pieces),
            "min_words": min(word_counts),
            "max_words": max(word_counts),
            "total_words": sum(word_counts),
        },
    )
    return pieces


# A fixed wrapper seed would reproduce the *same* bad audio on every regeneration,
# defeating the quality loop. So a regen attempt sends a distinct, deterministic
# seed override (per chunk + attempt); attempt 0 sends none, keeping the wrapper's
# configured seed for the reproducible baseline.
_REGEN_SEED_BASE = 0x9E3779B1


def _regen_seed(chunk_index: int, attempt: int) -> int:
    return (_REGEN_SEED_BASE + chunk_index * 131 + attempt) & 0xFFFFFFFF


async def _generate_chunk_quality_checked(
    job: jobs.Job,
    text: str,
    index: int,
    settings: Settings,
) -> tts.GenerateResult:
    """Synthesize one chunk, then (if enabled) check the audio and regenerate it
    when it came back degraded.

    Two independent checks gate a chunk, either of which can trigger a regen:
    the signal-level audio analysis (drone / noise / repetition) and, when
    WHISPER_VERIFY_ENABLED, an ASR divergence check that compares a faster-whisper
    transcript of the produced audio against the text we asked it to speak
    (catches dropout, hallucination, leaked preamble). Both share the
    AUDIO_ANALYSIS_MAX_REGEN budget.

    Chatterbox is non-deterministic, so a re-gen usually recovers. The wrapper
    overwrites the same ``{episode_id}_chunk_{index}.wav`` each call, so on
    persistent failure we keep the *last* attempt (earlier ones are gone from
    disk) and log a WARN -- a degraded chunk never fails the whole episode.
    Analysis errors are swallowed: they must never become a new failure mode."""

    word_count = len(text.split())
    audio_enabled = settings.AUDIO_ANALYSIS_ENABLED
    # Skip ASR on tiny chunks where transcription noise dominates the divergence.
    verify_enabled = (
        settings.WHISPER_VERIFY_ENABLED and word_count >= settings.WHISPER_VERIFY_MIN_WORDS
    )
    # max(0, ...) so a misconfigured negative regen count can't make the loop
    # body skip and return None (the field is operator-tunable at runtime).
    max_extra = (
        max(0, settings.AUDIO_ANALYSIS_MAX_REGEN) if (audio_enabled or verify_enabled) else 0
    )

    result = None
    last_reasons: list[str] = []
    for attempt in range(max_extra + 1):  # 1 baseline + up to max_extra regenerations
        result = await tts.generate_chunk_with_retry(
            text=text,
            episode_id=job.episode_id,
            chunk_index=index,
            settings=settings,
            seed=None if attempt == 0 else _regen_seed(index, attempt),
            verify=verify_enabled,
        )
        if not (audio_enabled or verify_enabled):
            return result

        reasons: list[str] = []
        verdict = None
        if audio_enabled:
            try:
                verdict = audio_analysis.analyze_wav_path(result.wav_path, word_count, settings)
            except audio.AudioError as exc:
                logger.warning(
                    "Chunk audio analysis failed; passing chunk through",
                    extra={"event": "chunk_analysis_error", "chunk_index": index, "error": str(exc)},
                )
                return result
            if not verdict.ok:
                reasons.extend(verdict.reasons)

        asr_div: float | None = None
        if verify_enabled and result.transcript is not None:
            asr_div = asr_verify.divergence(text, result.transcript)
            if asr_div > settings.WHISPER_DIVERGENCE_THRESHOLD:
                reasons.append("asr_divergence")

        last_reasons = reasons
        if not reasons:
            if attempt > 0:
                logger.info(
                    "Chunk passed after regeneration",
                    extra={
                        "event": "chunk_regen_recovered",
                        "chunk_index": index,
                        "attempts": attempt + 1,
                    },
                )
            return result
        log_extra: dict[str, Any] = {
            "event": "chunk_quality_bad",
            "chunk_index": index,
            "attempt": attempt + 1,
            "reasons": reasons,
            "asr_divergence": asr_div,
        }
        if verdict is not None:
            log_extra.update(
                rms_cv=verdict.metrics.rms_cv,
                crest_factor=verdict.metrics.crest_factor,
                zero_crossing_rate=verdict.metrics.zero_crossing_rate,
                silent_fraction=verdict.metrics.silent_fraction,
                duration_ratio=verdict.metrics.duration_ratio,
            )
        logger.warning("Bad chunk audio detected", extra=log_extra)

    logger.warning(
        "Chunk still degraded after max regenerations; keeping last attempt",
        extra={
            "event": "chunk_quality_unresolved",
            "chunk_index": index,
            "attempts": max_extra + 1,
            "reasons": last_reasons,
        },
    )
    return result


async def _stage_tts(
    job: jobs.Job, chunks: list[str], settings: Settings
) -> list[tts.GenerateResult]:
    """For each chunk, POST to the wrapper with client-side retry on
    transient failures. Returns the list of GenerateResult so the audio
    stage can read the per-chunk WAVs."""

    # Select this job's reference voice before the first chunk so it conditions on the
    # right voice. A job with no recorded slot (a pre-slots row or a reprocess/requeue
    # carrying None), or one whose recorded slot was deleted after submit, falls back to
    # the default (lowest filled) slot so it never inherits a slot left selected by an
    # earlier job or an audition. Only when no slot exists at all is selection skipped --
    # the wrapper then 503s and the job fails loudly rather than using a stale voice.
    # ``target_slot`` is the slot that selection actually settled on; it is re-asserted
    # before every later chunk (see the loop) so a concurrent audition's /reload can't
    # leave the rest of the episode in a different voice.
    target_slot: int | None = None
    if job.voice_id:
        try:
            slot = int(job.voice_id)
            await tts.select_voice(settings, slot)
            target_slot = slot
        except (ValueError, tts.TTSError):
            logger.warning(
                "Voice select failed; falling back to the default slot",
                extra={"event": "tts_select_voice_failed", "voice_id": job.voice_id},
            )
    if target_slot is None:
        default = voices.default_slot()
        if default is not None:
            try:
                await tts.select_voice(settings, default)
                target_slot = default
            except (ValueError, tts.TTSError):
                logger.warning(
                    "Default voice select failed; using the wrapper's current voice",
                    extra={"event": "tts_default_voice_failed", "slot": default},
                )

    results: list[tts.GenerateResult] = []
    total = len(chunks)
    for index, text in enumerate(chunks):
        _raise_if_cancelled(job.id, settings)
        if index > 0 and target_slot is not None:
            # Re-assert the voice before each later chunk so a concurrent slot audition's
            # /reload can't leave the rest of the episode in a different voice (a single
            # chunk can still race between this select and its generate). The wrapper
            # skips the re-encode when the slot is already active, so this is cheap.
            try:
                await tts.select_voice(settings, target_slot)
            except (ValueError, tts.TTSError):
                logger.warning(
                    "Voice re-select failed; continuing with the wrapper's current voice",
                    extra={"event": "tts_reselect_failed", "slot": target_slot},
                )
        result = await _generate_chunk_quality_checked(job, text, index, settings)
        results.append(result)
        _set_progress(job.id, index + 1, total, settings)
        logger.info(
            "Chunk synthesized",
            extra={
                "event": "tts_chunk_done",
                "chunk_index": index,
                "duration_secs": result.duration_secs,
                "wav_path": result.wav_path,
            },
        )
    logger.info(
        "TTS complete",
        extra={
            "event": "tts_stage_complete",
            "chunk_count": len(results),
            "total_audio_secs": sum(r.duration_secs for r in results),
        },
    )
    return results


async def _stage_audio(
    job: jobs.Job,
    chunk_results: list[tts.GenerateResult],
    settings: Settings,
) -> audio.EncodeResult:
    """Trim / concat / normalize / encode the per-chunk WAVs into an MP3.

    Removes per-chunk WAVs and the concatenated WAV on both success and
    failure -- no persistent debug artifacts.
    """

    out_root = media_dir(settings)
    chunk_paths = [Path(r.wav_path) for r in chunk_results]
    combined_path = out_root / f"{job.episode_id}_combined.wav"
    mp3_path = out_root / f"{job.episode_id}.mp3"

    try:
        audio.concat_with_padding(chunk_paths, combined_path, settings)
        # Optional end-of-episode chime: appended before normalization so it is
        # loudness-matched to the episode. Kept off unless enabled AND a clip exists.
        # A chime is decorative -- a bad/mismatched clip must not fail the episode, so a
        # failure here logs and ships the episode without the chime.
        chime_path = out_root / "chime.wav"
        if settings.CHIME_ENABLED and chime_path.is_file():
            try:
                audio.append_clip(combined_path, chime_path)
            except audio.AudioError:
                logger.warning(
                    "Chime append failed; shipping episode without it",
                    extra={"event": "chime_append_failed"},
                    exc_info=True,
                )
        result = audio.normalize_and_encode(combined_path, mp3_path, settings)
        logger.info(
            "Audio pipeline complete",
            extra={
                "event": "audio_complete",
                "mp3_path": str(result.mp3_path),
                "duration_secs": result.duration_secs,
            },
        )
        return result
    finally:
        # Per-chunk WAVs + concatenated WAV are not persistent artifacts.
        audio.remove_quietly(combined_path, *chunk_paths)


async def _stage_artwork(
    job: jobs.Job,
    metadata: dict[str, Any],
    settings: Settings,
) -> artwork.ArtworkResult | None:
    """Download + process the article's og:image.

    Never raises -- returns None on any documented failure so the pipeline
    advances to transcript regardless. RSS renders the feed-level
    artwork for episodes with no per-episode JPG on disk.
    """

    result = await artwork.process_artwork(metadata, job.episode_id, media_dir(settings), settings)
    if result is None:
        logger.info(
            "Artwork falling back to feed-level art",
            extra={"event": "artwork_fallback_to_feed"},
        )
    return result


async def _stage_transcript(
    chunks: list[str],
    chunk_results: list[tts.GenerateResult],
    settings: Settings,
) -> str:
    """Render the WebVTT transcript from chunk text + per-chunk durations."""

    if len(chunks) != len(chunk_results):
        # Explicit so the failure message in jobs.error names both sides --
        # zip(strict=True) would surface a stdlib "argument 2 is shorter"
        # which is harder to triage from an ops dashboard.
        raise ValueError(
            f"transcript stage: {len(chunks)} chunks but {len(chunk_results)} "
            f"TTS results -- pipeline state corrupted"
        )
    transcript_chunks = [
        transcript.TranscriptChunk(text=text, duration_secs=res.duration_secs)
        for text, res in zip(chunks, chunk_results, strict=False)
    ]
    vtt = transcript.build_vtt(transcript_chunks, settings.TTS_CHUNK_SILENCE_MS)
    logger.info(
        "Transcript rendered",
        extra={
            "event": "transcript_complete",
            "cue_count": len(transcript_chunks),
            "vtt_bytes": len(vtt.encode("utf-8")),
        },
    )
    return vtt


async def _stage_finalize(
    job: jobs.Job,
    *,
    metadata: dict[str, Any],
    audio_result: audio.EncodeResult,
    artwork_result: artwork.ArtworkResult | None,
    vtt: str,
    summary: str | None,
    cleaned_text: str | None,
    settings: Settings,
) -> None:
    """Upsert the ``episodes`` row that the RSS feed and media handlers read.

    Title and author come from the extraction metadata (Firecrawl populates
    both when the article has them); ``original_url`` is the job's input
    URL; durations come from the audio stage; ``transcript_vtt`` is the
    in-memory VTT rendered in the prior stage. ``cleaned_text`` is the
    post-corrections article (the exact text fed to chunking/TTS), persisted so
    the API can serve it; ``audio_size_bytes`` is stamped here to avoid stat()
    per request on the feed/episodes hot paths.
    """

    title = _coerce_str(metadata.get("title"))
    author = _coerce_str(metadata.get("author")) or settings.FEED_AUTHOR
    artwork_path = str(artwork_result.jpg_path) if artwork_result else None
    duration_secs = round(audio_result.duration_secs)
    audio_size_bytes = file_size_or_zero(str(audio_result.mp3_path))

    # Provenance: an uploaded document's ``original_url`` is the synthetic
    # ``upload://`` identifier; record source_type/source_filename so the feed and
    # UI render the filename instead of treating it as a hyperlink.
    if file_extraction.is_upload_source(job.url):
        source_type = "upload"
        source_filename = file_extraction.parse_source_uri(job.url)[1]
    else:
        source_type = "url"
        source_filename = None

    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        # Snapshot the voice this job used (slot label, "Slot N", or "Default")
        # so the API, feed UI, and RSS description can show which voice narrated it.
        voice_label = voices.label_for(conn, job.voice_id)
        episodes.upsert(
            conn,
            id=job.episode_id,
            job_id=job.id,
            original_url=job.url,
            title=title,
            author=author,
            audio_path=str(audio_result.mp3_path),
            artwork_path=artwork_path,
            transcript_vtt=vtt,
            duration_secs=duration_secs,
            summary=summary,
            cleaned_text=cleaned_text,
            audio_size_bytes=audio_size_bytes,
            source_type=source_type,
            source_filename=source_filename,
            voice_label=voice_label,
        )
    finally:
        conn.close()

    logger.info(
        "Episode finalized",
        extra={
            "event": "finalize_complete",
            "episode_id": job.episode_id,
            "title": title,
            "duration_secs": duration_secs,
            "has_artwork": artwork_path is not None,
            "vtt_bytes": len(vtt.encode("utf-8")),
        },
    )


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _apply_base_lexicon(text: str, conn, settings: Settings) -> str:
    """Aggressive per-token apply of the base lexicon.

    Every plain word is looked up; a ``base`` entry whose respelling differs and
    clears the confidence gate is applied. user/seed rows already ran via the
    regex pass, so only ``base`` rows are applied here. No-op when
    ``LEXICON_AGGRESSIVE`` is off or the base layer is empty.
    """

    if not settings.LEXICON_AGGRESSIVE:
        return text
    cache: dict[str, str] = {}

    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        if token in cache:
            return cache[token]
        entry = lexicon.lookup(conn, token)
        out = token
        if (
            entry is not None
            and entry.origin == "base"
            and entry.confidence >= pronounce_convert.MIN_CONFIDENCE
            and entry.spoken
            and entry.spoken != token
        ):
            out = entry.spoken
        cache[token] = out
        return out

    return lexicon.WORD_TOKEN_RE.sub(repl, text)


async def _apply_corrections(text: str, settings: Settings) -> str:
    """Deterministic normalization backstop, run after the LLM pronunciation pass.

    Order: regex fixups (``_normalize_for_tts``), the user+seed pronunciation
    dictionary (longest-key-first regex), the aggressive per-token base-lexicon
    pass, then the snake_case sweep. All pronunciation data is sourced from the
    ``lexicon`` table. This is the guaranteed coverage layer: anything the LLM pass
    missed is corrected here.
    """

    normalized = _normalize_for_tts(text)
    with database.connection(settings.DATA_DIR) as conn:
        cs_pairs, ci_pairs = lexicon.apply_pairs_by_case(conn)
        # Acronyms get no automatic letter-spelling: Chatterbox (BPE/char-based, no g2p)
        # pronounces common ones natively, and forcing "C E O" makes it choppy. A dictionary
        # entry (seed or user) is the escape hatch for any acronym it should say differently.
        # Explicit pronunciations from the dictionary apply here: exact-case keys first,
        # then the case-insensitive group folds so "404 media" hits "404 Media". Then the
        # aggressive base-lexicon pass and the snake_case sweep.
        applied = corrections.apply(normalized, cs_pairs)
        applied = corrections.apply(applied, ci_pairs, case_sensitive=False)
        applied = _apply_base_lexicon(applied, conn, settings)
    # Strip periods from dotted acronyms LAST -- catches both article text ("U.S.")
    # and any dotted respelling a correction injected ("A.I.") -- so the engine
    # never pauses mid-acronym.
    result = _normalize_dotted_acronyms(_normalize_identifiers(applied))
    logger.info(
        "Corrections applied",
        extra={
            "event": "corrections_complete",
            "entries_pairs": len(cs_pairs) + len(ci_pairs),
            "delta_chars": len(result) - len(text),
        },
    )
    return result


# A pronunciation pass only respells terms, so each window's output should be
# about as long as its input. A far-shorter return signals truncation or a
# refusal; below this ratio the window falls back to its input so the pass can
# only improve pronunciation, never drop article content.
_PRONUNCIATION_MIN_RATIO = 0.5


async def _pronounce_with_llm(job_id: str, text: str, settings: Settings) -> str:
    """LLM pronunciation pass: respell terms from the full correction set (seed +
    user dictionary) by context, leaving everything else verbatim.

    Per-window like cleanup so a long article never hits the output-token cap.
    Degrades safely: a failed reference load or a failed/short window passes that
    text through unchanged. The deterministic backstop in ``_apply_corrections``
    still runs after, so a skipped window is never left uncorrected.
    """

    with database.connection(settings.DATA_DIR) as conn:
        system_prompt = prompt_service.load_effective(conn, "pronunciation")
        try:
            reference = lexicon.reference_text(conn)
        except Exception:
            logger.error(
                "Pronunciation reference failed to load; skipping LLM pass",
                extra={"event": "pronunciation_reference_load_failed"},
                exc_info=True,
            )
            return text
    if not reference:
        return text
    system_prompt = (
        f"{system_prompt}\n\nPRONUNCIATION REFERENCE (term -> respelling):\n{reference}"
    )

    windows = chunker.pack_paragraphs(text, settings.LLM_CLEANUP_WINDOW_CHARS) or [text]
    out_parts: list[str] = []
    for index, window in enumerate(windows):
        # Same marker contract as cleanup: wrap the output so any preamble the
        # model glues on ("...here is the text reproduced in full:") lands outside
        # the markers and is sliced off by extract_clean_output -- the min-ratio
        # guard below only catches short output, not preamble-bloated output.
        user_message = (
            "Reproduce the text below in full, copying every sentence in order and "
            "changing only the spelled form of terms that match the pronunciation "
            f"reference. Output ONLY the text between a line {cleanup_output.BEGIN_MARKER} "
            f"and a line {cleanup_output.END_MARKER} -- no commentary outside the markers."
            f"\n\n<text>\n{window}\n</text>"
        )
        try:
            raw = await _llm_with_retry(system_prompt, user_message, settings)
            if cleanup_output.BEGIN_MARKER not in raw:
                # Model ignored the marker contract; one stern retry to force it.
                raw = await _llm_with_retry(
                    system_prompt,
                    "Your previous reply was rejected. Output ONLY the reproduced "
                    f"text between {cleanup_output.BEGIN_MARKER} and "
                    f"{cleanup_output.END_MARKER}, with no other words.\n\n" + user_message,
                    settings,
                )
            # Trust only marker-delimited output. If the model defied the contract
            # twice, keep the window verbatim rather than run extract_clean_output's
            # preamble heuristic, which is tuned for cleanup and could drop a real
            # paragraph opening like a preamble ("Here is...") or miss an unrecognized
            # one; the deterministic backstop still respells this window afterward.
            part = (
                cleanup_output.extract_clean_output(raw)
                if cleanup_output.BEGIN_MARKER in raw
                else window
            )
        except Exception:
            logger.warning(
                "Pronunciation window failed; passing it through unchanged",
                extra={"event": "pronunciation_window_failed", "window_index": index},
                exc_info=True,
            )
            part = window
        if len(part) < len(window) * _PRONUNCIATION_MIN_RATIO:
            logger.warning(
                "Pronunciation window output too short; keeping original",
                extra={
                    "event": "pronunciation_window_short",
                    "window_index": index,
                    "input_chars": len(window),
                    "output_chars": len(part),
                    # Snippet of what the model returned, to diagnose a short reply
                    # (e.g. a refusal or "nothing to respell") vs a real respelling.
                    "output_preview": part[:200],
                },
            )
            part = window
        out_parts.append(part)
        _set_progress(job_id, index + 1, len(windows), settings)
    return "\n\n".join(out_parts)


async def _stage_normalize(job: jobs.Job, cleaned: str, settings: Settings) -> str:
    """Dedicated pronunciation + normalization phase (post-cleanup, pre-chunk).

    Two layers: an LLM pronunciation pass that respells terms from the full
    correction set by context, then the deterministic ``_apply_corrections``
    backstop (regex fixups + seed/user dictionary) that guarantees coverage for
    anything the LLM missed.
    """

    pronounced = await _pronounce_with_llm(job.id, cleaned, settings)
    result = await _apply_corrections(pronounced, settings)
    logger.info(
        "Normalize stage complete",
        extra={
            "event": "normalize_complete",
            "input_chars": len(cleaned),
            "output_chars": len(result),
        },
    )
    return result


async def _stage_summary(text: str, settings: Settings) -> str | None:
    """Generate a short show-notes summary of the cleaned narration text.

    Never fails the job: show notes are non-essential, so any error returns None
    and the episode still publishes with the minimal title/author/source
    description. The exception is logged with its traceback (exc_info), so a real
    bug stays visible in the logs rather than being silently lost.
    """

    with database.connection(settings.DATA_DIR) as conn:
        system_prompt = prompt_service.load_effective(conn, "summary")
    # A 2-4 sentence show-notes blurb only needs the article opening, so cap the
    # input -- most articles fit under the cap (no change), and a very long one
    # is summarized from its first ~16K chars instead of billing the full body
    # as input tokens. (Intentionally not windowed like cleanup: the output is
    # tiny and the cap bounds the input.)
    head = text[:_SUMMARY_MAX_INPUT_CHARS]
    user_message = (
        "Summarize the article below per your instructions. Return ONLY the "
        "summary sentences -- no commentary, no greetings, no questions."
        f"\n\n<article>\n{head}\n</article>"
    )
    try:
        summary = (await _llm_with_retry(system_prompt, user_message, settings)).strip()
    except Exception:
        logger.warning(
            "Summary generation failed; episode publishes without show notes",
            extra={"event": "summary_failed"},
            exc_info=True,
        )
        return None
    logger.info(
        "Summary generated",
        extra={"event": "summary_complete", "summary_chars": len(summary)},
    )
    return summary or None


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


def _set_progress(job_id: str, current: int, total: int, settings: Settings) -> None:
    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        jobs.set_progress(conn, job_id, current, total)
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
    job = _load_job(job_id, settings)
    return job.stage if job else None
