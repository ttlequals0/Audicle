"""Source acquisition for directly-uploaded documents (0.30.0).

The URL pipeline fetches an article via Firecrawl; an uploaded document instead
lives on disk and is parsed here. This module is the file-source counterpart to
``extraction.extract`` -- it returns the same ``ExtractionResult`` (markdown +
metadata) so every downstream stage (cleanup -> normalize -> ... -> finalize) is
unchanged.

An upload is represented end-to-end as a job whose ``url`` is a synthetic
identifier ``upload://{content_hash}/{quoted_filename}``. The hash makes the
episode id deterministic from the file's bytes+name (so a re-upload dedupes like
a URL re-submit), and the filename carries the type (for the parser) and a title
fallback. The original bytes are stored at ``{media}/{episode_id}.source{ext}``
so a reprocess can re-read them with no re-upload.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from app.config import Settings
from app.core.paths import media_dir
from app.services import html_markdown
from app.services.extraction_types import (
    ExtractionPermanentError,
    ExtractionResult,
    ExtractionTooShortError,
)
from app.services.jobs import Job

logger = logging.getLogger("app.services.file_extraction")

SOURCE_SCHEME = "upload://"

# Accepted upload extensions, mapped to a parser. ``.htm`` is an alias for
# ``.html``. Each type must have a parser here, so the allowlist is a code
# constant (not an operator setting): adding a type without a parser would
# accept files the pipeline then can't read.
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".pdf", ".docx", ".md", ".txt", ".html", ".htm"})

# Strip directory components and control characters from a client-supplied
# filename before it is embedded in the synthetic URI / used to derive a path.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_MAX_FILENAME_LEN = 255


def is_upload_source(url: str) -> bool:
    """True if ``url`` is a synthetic upload identifier rather than a real URL."""

    return url.startswith(SOURCE_SCHEME)


def sanitize_filename(name: str) -> str:
    """Reduce a client-supplied filename to a safe basename.

    Drops any path components (``../etc/passwd`` -> ``passwd``), strips control
    characters, and caps the length. The result is only ever used inside the
    synthetic URI and to derive the ``.source`` extension -- the on-disk path is
    keyed by ``episode_id``, not this name -- but it is sanitized defensively.
    """

    base = Path(name.replace("\\", "/")).name
    base = _CONTROL_CHARS.sub("", base).strip()
    return base[:_MAX_FILENAME_LEN]


def extension_of(filename: str) -> str:
    """Lower-cased file extension including the leading dot (``.pdf``)."""

    return Path(filename).suffix.lower()


def build_source_uri(content_hash: str, filename: str) -> str:
    """Synthetic source identifier carried in ``jobs.url`` / ``episodes.original_url``."""

    return f"{SOURCE_SCHEME}{content_hash}/{quote(filename, safe='')}"


def parse_source_uri(uri: str) -> tuple[str, str]:
    """Inverse of :func:`build_source_uri`: ``(content_hash, filename)``."""

    body = uri[len(SOURCE_SCHEME) :]
    content_hash, _, raw_name = body.partition("/")
    return content_hash, unquote(raw_name)


def source_path(settings: Settings, episode_id: str, filename: str) -> Path:
    """Where the original upload is stored: ``{media}/{episode_id}.source{ext}``."""

    return media_dir(settings) / f"{episode_id}.source{extension_of(filename)}"


async def extract_file(job: Job, settings: Settings) -> ExtractionResult:
    """Read and parse the stored upload for ``job`` into article markdown + metadata.

    Mirrors ``extraction.extract``'s contract. The CPU-bound parse runs in a
    worker thread so the job-timeout/cancellation can still fire during a large
    PDF parse.
    """

    _, filename = parse_source_uri(job.url)
    ext = extension_of(filename)
    path = source_path(settings, job.episode_id, filename)
    if not path.exists():
        raise ExtractionPermanentError(
            f"stored upload missing for episode {job.episode_id} ({path.name}); "
            "re-upload the document to reprocess it"
        )

    # Read + parse off the event loop so a large file on slow storage can't block it.
    markdown, metadata = await asyncio.to_thread(lambda: _parse(path.read_bytes(), ext))
    markdown = markdown.strip()

    # Filename (minus extension) is the title fallback when the document declares
    # none -- the common case for PDFs, plain text, and bare markdown.
    if not metadata.get("title"):
        stem = Path(filename).stem.strip()
        if stem:
            metadata["title"] = stem

    floor = settings.MIN_EXTRACTION_CHARS
    if len(markdown) < floor:
        raise ExtractionTooShortError(
            f"uploaded {ext or 'file'} yielded only {len(markdown)} characters of text "
            f"(minimum {floor}); a scanned or image-only document has no extractable text"
        )

    logger.info(
        "File extraction succeeded",
        extra={
            "event": "file_extract_complete",
            "ext": ext,
            "markdown_chars": len(markdown),
            "has_title": bool(metadata.get("title")),
        },
    )
    return ExtractionResult(markdown=markdown, metadata=metadata, article_chars=None)


def _parse(data: bytes, ext: str) -> tuple[str, dict[str, Any]]:
    """Dispatch raw bytes to the per-format parser. Returns ``(markdown, metadata)``."""

    if ext == ".pdf":
        return _parse_pdf(data)
    if ext == ".docx":
        return _parse_docx(data)
    if ext in (".html", ".htm"):
        return html_markdown.html_to_markdown(data.decode("utf-8", errors="replace"))
    # .md / .txt (and any allowed text type): the body is the text itself.
    return _parse_text(data)


def _parse_pdf(data: bytes) -> tuple[str, dict[str, Any]]:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except (PyPdfError, ValueError, OSError) as exc:
        raise ExtractionPermanentError(f"could not read PDF: {exc}") from exc
    markdown = "\n\n".join(p.strip() for p in pages if p.strip())
    metadata: dict[str, Any] = {}
    info = reader.metadata
    if info is not None:
        if info.title:
            metadata["title"] = str(info.title)
        if info.author:
            metadata["author"] = str(info.author)
    return markdown, metadata


def _parse_docx(data: bytes) -> tuple[str, dict[str, Any]]:
    import docx
    from docx.opc.exceptions import PackageNotFoundError

    try:
        document = docx.Document(io.BytesIO(data))
    except (PackageNotFoundError, ValueError, KeyError, OSError) as exc:
        raise ExtractionPermanentError(f"could not read DOCX: {exc}") from exc
    markdown = "\n\n".join(p.text.strip() for p in document.paragraphs if p.text.strip())
    metadata: dict[str, Any] = {}
    props = document.core_properties
    if props.title:
        metadata["title"] = props.title
    if props.author:
        metadata["author"] = props.author
    return markdown, metadata


def _parse_text(data: bytes) -> tuple[str, dict[str, Any]]:
    text = data.decode("utf-8", errors="replace")
    metadata: dict[str, Any] = {}
    title = _first_markdown_heading(text)
    if title:
        metadata["title"] = title
    return text, metadata


def _first_markdown_heading(text: str) -> str | None:
    """Return the first ``# H1`` heading's text, if the document opens with one."""

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            return stripped[2:].strip() or None
        return None  # first non-blank line isn't an H1
    return None
