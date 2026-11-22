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
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import docx
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from app.config import Settings
from app.core import database
from app.core.paths import media_dir
from app.services import book_chapters, books, html_markdown, ocr
from app.services.book_chapters import BookFormatError, page_has_image
from app.services.extraction_types import (
    ExtractionPermanentError,
    ExtractionResult,
    ExtractionTooShortError,
)
from app.services.jobs import Job

logger = logging.getLogger("app.services.file_extraction")

SOURCE_SCHEME = "upload://"

# Image uploads route straight to OCR (0.51.0); they are only accepted while
# OCR_ENABLED is on, checked at parse time so the toggle needs no restart.
IMAGE_EXTENSIONS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp", ".tiff")

# Accepted upload extensions, mapped to a parser. ``.htm`` is an alias for
# ``.html``. Each type must have a parser here, so the allowlist is a code
# constant (not an operator setting): adding a type without a parser would
# accept files the pipeline then can't read.
ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {".pdf", ".docx", ".epub", ".md", ".txt", ".html", ".htm"} | set(IMAGE_EXTENSIONS)
)

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

    body = books.split_chapter_uri(uri)[0][len(SOURCE_SCHEME) :]
    content_hash, _, raw_name = body.partition("/")
    return content_hash, unquote(raw_name)


def source_path(settings: Settings, episode_id: str, filename: str) -> Path:
    """Where the original upload is stored: ``{media}/{episode_id}.source{ext}``."""

    return media_dir(settings) / f"{episode_id}.source{extension_of(filename)}"


def stored_source_path(settings: Settings, url: str, episode_id: str) -> Path:
    """Where the original for ``url`` lives: the book source for a chapter, else the upload."""

    base_uri, chapter_idx = books.split_chapter_uri(url)
    _, filename = parse_source_uri(base_uri)
    if chapter_idx is None:
        return source_path(settings, episode_id, filename)
    return books.source_file(settings, books.book_id_for(base_uri), extension_of(filename))


def _load_chapter(
    settings: Settings, base_uri: str, idx: int, episode_id: str
) -> tuple[books.Book, books.Chapter]:
    """Look up a chapter's book and row, closing the connection before any parsing."""

    book_id = books.book_id_for(base_uri)
    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        found = books.chapter_for_episode(conn, episode_id)
    finally:
        conn.close()
    if found is None or found[1].idx != idx:
        raise ExtractionPermanentError(
            f"stored upload missing for book {book_id} (chapter {idx}); "
            "re-upload the book to reprocess it"
        )
    return found


async def extract_file(
    job: Job, settings: Settings, beat: Callable[[], None] = lambda: None
) -> ExtractionResult:
    """Read and parse the stored upload for ``job`` into article markdown + metadata.

    Mirrors ``extraction.extract``'s contract. The CPU-bound parse runs in a
    worker thread so the job-timeout/cancellation can still fire during a large
    PDF parse. ``beat`` is therefore called from that worker thread, not the
    loop, so whatever is passed has to tolerate it.
    """

    base_uri, chapter_idx = books.split_chapter_uri(job.url)
    _, filename = parse_source_uri(base_uri)
    ext = extension_of(filename)
    found = (
        _load_chapter(settings, base_uri, chapter_idx, job.episode_id)
        if chapter_idx is not None
        else None
    )
    path = stored_source_path(settings, job.url, job.episode_id)
    if not path.exists():
        missing = f"book {found[0].id}" if found else f"episode {job.episode_id}"
        raise ExtractionPermanentError(
            f"stored upload missing for {missing} ({path.name}); "
            "re-upload the document to reprocess it"
        )

    # Read + parse off the event loop so a large file on slow storage can't block
    # it. asyncio.to_thread copies the context, so the per-page OCR beat reaches
    # the running job's watchdog.
    markdown, metadata = await asyncio.to_thread(
        lambda: _parse(path.read_bytes(), ext, settings, beat, found[1].locator if found else None)
    )
    markdown = markdown.strip()
    if found:
        metadata = _chapter_metadata(settings, *found)

    # Filename (minus extension) is the title fallback when the document declares
    # none -- the common case for PDFs, plain text, and bare markdown.
    if not metadata.get("title"):
        stem = Path(filename).stem.strip()
        if stem:
            metadata["title"] = stem

    floor = settings.MIN_EXTRACTION_CHARS
    if len(markdown) < floor:
        reason = (
            "scanned pages require OCR_ENABLED; blank pages contain no readable text"
            if ext == ".pdf"
            else "the document has no extractable text"
        )
        raise ExtractionTooShortError(
            f"uploaded {ext or 'file'} yielded only {len(markdown)} characters of text "
            f"(minimum {floor}); {reason}"
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


def _chapter_metadata(
    settings: Settings, book: books.Book, chapter: books.Chapter
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "title": f"{book.title}: {chapter.title}" if book.title else chapter.title,
        "author": book.author,
        "season_number": book.season_number,
        "episode_number": chapter.idx,
    }
    if book.has_cover:
        metadata["book_cover_path"] = str(books.cover_file(settings, book.id))
    return metadata


def _parse(
    data: bytes,
    ext: str,
    settings: Settings,
    beat: Callable[[], None],
    locator: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Dispatch raw bytes to the per-format parser. Returns ``(markdown, metadata)``.

    ``locator`` restricts a book source to one chapter (EPUB spine range or PDF page range).
    """

    if ext == ".epub":
        try:
            if locator is not None:
                return book_chapters.epub_chapter_markdown(data, locator), {}
            return book_chapters.epub_full_markdown(data)
        except BookFormatError as exc:
            raise ExtractionPermanentError(f"could not read EPUB: {exc}") from exc
    if ext == ".pdf":
        # Page indexes below are relative to the range; ``first`` maps them back for OCR.
        first = locator["first_page"] if locator is not None else 0
        pages, image_pages, metadata = (
            _parse_pdf(data) if locator is None else _parse_pdf(data, first, locator["last_page"])
        )
        markdown = "\n\n".join(page for page in pages if page)
        sparse_image_pages = [
            index
            for index in image_pages
            if len(pages[index].strip()) < settings.MIN_EXTRACTION_CHARS
        ]
        if sparse_image_pages and not settings.OCR_ENABLED:
            raise ExtractionPermanentError(
                "Scanned PDF pages require OCR. Enable OCR_ENABLED to read the complete document."
            )
        if settings.OCR_ENABLED:
            if sparse_image_pages:
                logger.info(
                    "PDF contains sparse image pages; running OCR",
                    extra={
                        "event": "ocr_fallback_engaged",
                        "pypdf_chars": len(markdown),
                        "pages": len(sparse_image_pages),
                    },
                )
            try:
                recognized = (
                    ocr.ocr_pdf_pages(data, settings, beat, [first + i for i in sparse_image_pages])
                    if sparse_image_pages
                    else {}
                )
            except ocr.OcrError as exc:
                raise ExtractionPermanentError(f"Could not read scanned PDF pages: {exc}") from exc
            for absolute, text in recognized.items():
                index = absolute - first
                if text.strip():
                    pages[index] = "\n\n".join(part for part in (pages[index], text) if part)
            markdown = "\n\n".join(page for page in pages if page)
        return markdown, metadata
    if ext in IMAGE_EXTENSIONS:
        if not settings.OCR_ENABLED:
            raise ExtractionPermanentError(
                "image uploads require OCR, which is disabled (OCR_ENABLED)"
            )
        try:
            return ocr.ocr_image(data, settings, beat), {}
        except ocr.OcrError as exc:
            raise ExtractionPermanentError(str(exc)) from exc
    if ext == ".docx":
        return _parse_docx(data)
    if ext in (".html", ".htm"):
        return html_markdown.html_to_markdown(data.decode("utf-8", errors="replace"))
    # .md / .txt (and any allowed text type): the body is the text itself.
    return _parse_text(data)


def _parse_pdf(
    data: bytes, first: int = 0, last: int | None = None
) -> tuple[list[str], set[int], dict[str, Any]]:
    try:
        reader = PdfReader(io.BytesIO(data))
        source_pages = list(reader.pages)[first : None if last is None else last + 1]
        pages = [page.extract_text() or "" for page in source_pages]
        image_pages = {index for index, page in enumerate(source_pages) if page_has_image(page)}
    except (PyPdfError, ValueError, OSError) as exc:
        raise ExtractionPermanentError(f"could not read PDF: {exc}") from exc
    pages = [page.strip() for page in pages]
    metadata: dict[str, Any] = {}
    info = reader.metadata
    if info is not None:
        if info.title:
            metadata["title"] = str(info.title)
        if info.author:
            metadata["author"] = str(info.author)
    return pages, image_pages, metadata


def _parse_docx(data: bytes) -> tuple[str, dict[str, Any]]:
    try:
        document = docx.Document(io.BytesIO(data))
    except (PackageNotFoundError, ValueError, KeyError, OSError) as exc:
        raise ExtractionPermanentError(f"could not read DOCX: {exc}") from exc

    def blocks(container: Any) -> list[str]:
        output: list[str] = []
        for child in container.iterchildren():
            if isinstance(child, CT_P):
                text = Paragraph(child, document).text.strip()
                if text:
                    output.append(text)
            elif isinstance(child, CT_Tbl):
                table = Table(child, document)
                seen_cells: set[Any] = set()
                for row in table.rows:
                    cells: list[str] = []
                    for cell in row.cells:
                        if cell._tc in seen_cells:
                            continue
                        seen_cells.add(cell._tc)
                        cells.extend(blocks(cell._tc))
                    line = " | ".join(cells)
                    if line:
                        output.append(line)
        return output

    blocks_ = blocks(document.element.body)
    markdown = "\n\n".join(blocks_)
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
