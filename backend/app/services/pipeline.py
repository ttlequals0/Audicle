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
- The whole job runs under ``asyncio.wait_for(JOB_TIMEOUT_SECONDS)``; the
  timeout handler reads the last persisted stage and writes a clear error.
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
    artwork,
    audio,
    chunker,
    corrections,
    episodes,
    extraction,
    jobs,
    llm,
    seed_corrections,
    transcript,
    tts,
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


async def process_job(job: jobs.Job, settings: Settings) -> None:
    """Run the configured pipeline stages against ``job``.

    On any failure write ``stage`` + ``error`` and set status=failed. On
    timeout report the last persisted stage in the error message.
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


async def _run_stages(job: jobs.Job, settings: Settings) -> None:
    """Run the stages in order: extract -> cleanup -> normalize -> summary ->
    chunk -> tts -> audio -> artwork -> transcript -> finalize (finalize
    inserts/updates the episodes row)."""

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
# shapes XTTS-v2 reliably garbles. One token must carry a comma group or a
# decimal point to match -- bare integers (15, 2026), unit-attached numbers
# (500m), versions (1.2.3), and code-glued digits (x86, startup_32) are
# deliberately left to the LLM prompt because they are context-dependent (a year
# reads "twenty twenty-six", an emergency number reads "nine one one"). The
# (?<![\w.,])/(?![\w.,]) guards keep this off identifiers and the middle of a
# dotted version string or IP address.
_SPELLABLE_NUMBER_RE = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+)(?![\w.,])"
)

# Money with a currency symbol and an optional magnitude suffix (k/m/b/t). The
# leading symbol is what disambiguates the suffix from a unit -- "$500m" is
# "five hundred million dollars" while a bare "500m north" stays meters (left to
# the LLM prompt). Trailing (?!\w) keeps "$500kg" out (k then g) so a mis-suffixed
# token falls through untouched rather than being read wrong.
_CURRENCY_RE = re.compile(
    r"(?<!\w)([$€£])\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)([kKmMbBtT])?(?!\w)"
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
        symbol, number, magnitude = match.group(1), match.group(2), match.group(3)
        words = _spell_number(number)
        if magnitude:
            words = f"{words} {_MAGNITUDE_WORDS[magnitude.lower()]}"
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
# as spaced words so XTTS doesn't hallucinate on the underscores. Dotted file
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
    """Remove inline-code noise XTTS can't voice: backticks, ``()`` call syntax,
    and hex literals (replaced with a spoken phrase)."""

    text = text.replace("`", "")
    text = _HEX_LITERAL_RE.sub("a hexadecimal value", text)
    return _EMPTY_CALL_PARENS_RE.sub("", text)


def _normalize_for_tts(text: str) -> str:
    """Deterministic fixups for things the cleanup prompt doesn't reliably catch.

    One ordered pass so future rules have a single home: strip residual markdown
    heading markers, strip inline-code artifacts (backticks, call-parens, hex),
    expand date-context month abbreviations, then spell the number shapes XTTS
    garbles. Runs at the end of cleanup, before the pronunciation dictionary, so
    e.g. "Feb 3" becomes "February 3" and the corrections dict can then voice it
    correctly. Code-artifact stripping runs before number spelling so a hex
    literal is gone before the number pass ever looks at it.
    """

    return _normalize_numbers(
        _normalize_currency(
            _normalize_date_months(_strip_code_artifacts(_strip_heading_markers(text)))
        )
    )


# The cleanup prompt tells the model to emit this exact token for a window that
# is all boilerplate (no article body). An explicit sentinel is obeyed far more
# reliably than "return nothing", which models tend to answer with a prose
# disclaimer ("There is no article content...") that would otherwise be narrated.
_EMPTY_SECTION_SENTINEL = "NO_ARTICLE_CONTENT"
# Backstops for when the model ignores the sentinel and writes a refusal in prose.
# Assistant-offer phrasing ("if you paste the article...") is conclusive on its
# own -- a real article never says it. The "there is no article..." opener can
# appear in a real column, so it additionally requires a reference to the supplied
# input (or a boilerplate keyword) before a window is dropped.
_DISCLAIMER_OFFER_RE = re.compile(
    r"^if you (paste|provide|share) the article", re.IGNORECASE
)
_DISCLAIMER_OPENER_RE = re.compile(
    r"^(there (is|was) no (article|content|text)|"
    r"no article (body|content|text) (was|is|were))",
    re.IGNORECASE,
)
_DISCLAIMER_SIGNAL_RE = re.compile(
    r"you (provided|shared|gave|pasted)|in (the content|what you)|"
    r"was not included|to extract|cookie|consent|boilerplate|navigation",
    re.IGNORECASE,
)


def _is_empty_section(text: str) -> bool:
    """True when a cleanup window produced no article body -- the sentinel
    (anywhere in the output, in case the model adds stray prose), or a short
    refusal the model wrote instead of the sentinel."""

    if _EMPTY_SECTION_SENTINEL in text:
        return True
    stripped = text.strip()
    # Backstop only: a short output (a real article section is long). The offer
    # phrasing is conclusive; the "no article" opener needs a supplied-input
    # signal so a real "There is no article this week..." column is not dropped.
    if len(stripped) >= 600:
        return False
    if _DISCLAIMER_OFFER_RE.match(stripped):
        return True
    return bool(_DISCLAIMER_OPENER_RE.match(stripped)) and bool(
        _DISCLAIMER_SIGNAL_RE.search(stripped)
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

    # Process the article in paragraph-bounded windows, one LLM call each, then
    # concatenate. A single giant call capped the output at LLM_MAX_TOKENS and
    # truncated long articles to the first paragraph; windowing keeps each call's
    # output well under the cap so article length is never the bottleneck.
    windows = chunker.pack_paragraphs(markdown, settings.LLM_CLEANUP_WINDOW_CHARS) or [markdown]
    cleaned_parts: list[str] = []
    for index, window in enumerate(windows):
        # Repeat the directive in the user turn (many models weight it higher
        # than the system prompt) and delimit the article so the model cleans it
        # rather than replying conversationally to it.
        user_message = (
            "Clean the article below per your instructions. Return ONLY the "
            "cleaned narration text -- no commentary, no greetings, no questions. "
            "If this passage has no article body, output exactly NO_ARTICLE_CONTENT "
            "and nothing else."
            f"\n\n<article>\n{window}\n</article>"
        )
        part = (await _llm_with_retry(system_prompt, user_message, settings)).strip()
        # Drop boilerplate-only windows so a "there is no article" disclaimer never
        # reaches narration; the empty string is filtered out of the join below.
        empty = _is_empty_section(part)
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


async def _stage_tts(
    job: jobs.Job, chunks: list[str], settings: Settings
) -> list[tts.GenerateResult]:
    """For each chunk, POST to the wrapper with client-side retry on
    transient failures. Returns the list of GenerateResult so the audio
    stage can read the per-chunk WAVs."""

    results: list[tts.GenerateResult] = []
    total = len(chunks)
    for index, text in enumerate(chunks):
        result = await tts.generate_chunk_with_retry(
            text=text,
            episode_id=job.episode_id,
            chunk_index=index,
            settings=settings,
        )
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

    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
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


async def _apply_corrections(
    text: str, settings: Settings, user_dict: dict[str, str] | None = None
) -> str:
    """Deterministic normalization backstop, run after the LLM pronunciation pass.

    Order: regex fixups (``_normalize_for_tts`` -- headings, code artifacts,
    dates, currency, numbers), then the seed + user pronunciation dictionary in
    one pass, then the snake_case identifier sweep. This is the guaranteed
    coverage layer: anything the LLM pass missed is corrected here.

    Seed and user dictionaries are re-read every call. The user dictionary wins on
    key collision (``{**seed, **user}``); ``_stage_normalize`` passes it in so it
    is loaded once per article, but it is loaded here when called directly. A
    malformed *bundled* seed CSV degrades to user-only rather than failing every
    job; a malformed *user* file still raises in ``corrections.load`` so the
    operator fixes their own file.
    """

    normalized = _normalize_for_tts(text)
    if user_dict is None:
        with database.connection(settings.DATA_DIR) as conn:
            user_dict = corrections.load_user_dict(conn)
    try:
        seed_dict = seed_corrections.load_applicable_dict()
    except Exception:
        logger.error(
            "Seed corrections failed to load; applying user corrections only",
            extra={"event": "seed_corrections_load_failed"},
            exc_info=True,
        )
        seed_dict = {}
    merged = {**seed_dict, **user_dict}
    # Explicit pronunciations first (so "ttyS0" -> "T T Y S 0" wins), then the
    # generic identifier transform mops up any snake_case/dotted-file token no
    # rule covered.
    result = _normalize_identifiers(corrections.apply(normalized, merged))
    logger.info(
        "Corrections applied",
        extra={
            "event": "corrections_complete",
            "entries_user": len(user_dict),
            "entries_seed_applicable": len(seed_dict),
            "entries_merged": len(merged),
            "delta_chars": len(result) - len(text),
        },
    )
    return result


# A pronunciation pass only respells terms, so each window's output should be
# about as long as its input. A far-shorter return signals truncation or a
# refusal; below this ratio the window falls back to its input so the pass can
# only improve pronunciation, never drop article content.
_PRONUNCIATION_MIN_RATIO = 0.5


async def _pronounce_with_llm(
    job_id: str, text: str, settings: Settings, user_dict: dict[str, str] | None = None
) -> str:
    """LLM pronunciation pass: respell terms from the full correction set (seed +
    user dictionary) by context, leaving everything else verbatim.

    Per-window like cleanup so a long article never hits the output-token cap.
    Degrades safely: a failed reference load or a failed/short window passes that
    text through unchanged. The deterministic backstop in ``_apply_corrections``
    still runs after, so a skipped window is never left uncorrected.
    """

    with database.connection(settings.DATA_DIR) as conn:
        system_prompt = prompt_service.load_effective(conn, "pronunciation")
        if user_dict is None:
            user_dict = corrections.load_user_dict(conn)
    try:
        reference = seed_corrections.load_reference(user_dict)
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
        user_message = (
            "Reproduce the text below in full, copying every sentence in order and "
            "changing only the spelled form of terms that match the pronunciation "
            "reference. Output the complete text and nothing else."
            f"\n\n<text>\n{window}\n</text>"
        )
        try:
            part = (await _llm_with_retry(system_prompt, user_message, settings)).strip()
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

    # Load the user dictionary once for both the LLM pass and the deterministic
    # backstop rather than opening a second connection in each.
    with database.connection(settings.DATA_DIR) as conn:
        user_dict = corrections.load_user_dict(conn)
    pronounced = await _pronounce_with_llm(job.id, cleaned, settings, user_dict=user_dict)
    result = await _apply_corrections(pronounced, settings, user_dict=user_dict)
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
    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        job = jobs.get_job(conn, job_id)
    finally:
        conn.close()
    return job.stage if job else None
