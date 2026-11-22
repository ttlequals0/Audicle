"""Create the stored book for a chapter-split upload (source, cover, rows) once per file."""

from __future__ import annotations

import asyncio
import io
import shutil
import sqlite3

from PIL import Image, UnidentifiedImageError

from app.config import Settings
from app.core import database
from app.core.paths import book_dir
from app.services import book_chapters, books
from app.services.atomic_write import write_bytes_atomic

_COVER_FORMATS = ("JPEG", "PNG", "GIF", "WEBP")

Prepared = tuple[book_chapters.DetectedBook, bytes | None]


class BookChangedError(Exception):
    """The book was deleted or recreated twice while the upload was being stored."""


def _cover_jpeg(raw: bytes) -> bytes | None:
    """Re-encode a cover as JPEG; None when unreadable or over the pixel limit."""

    try:
        image = Image.open(io.BytesIO(raw), formats=_COVER_FORMATS)
        limit = Image.MAX_IMAGE_PIXELS
        if limit and image.width * image.height > limit:
            return None
        out = io.BytesIO()
        image.convert("RGB").save(out, format="JPEG")
        return out.getvalue()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        return None


def _prepare(data: bytes, ext: str, min_chars: int) -> Prepared | None:
    """CPU-bound: detect chapters and prepare the cover. None when no chapters were found."""

    detected = book_chapters.detect(data, ext, min_chars=min_chars)
    if detected is None:
        return None
    return detected, _cover_jpeg(detected.cover) if detected.cover else None


def _write_book(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    data: bytes,
    ext: str,
    source_uri: str,
    filename: str,
    prepared: Prepared,
) -> books.Book:
    detected, cover = prepared
    book_id = books.book_id_for(source_uri)
    try:
        write_bytes_atomic(books.source_file(settings, book_id, ext), data)
        if cover is not None:
            write_bytes_atomic(books.cover_file(settings, book_id), cover)
        return books.create_book(
            conn,
            source_uri=source_uri,
            filename=filename,
            detected=detected,
            has_cover=cover is not None,
        )
    except Exception:
        shutil.rmtree(book_dir(settings, book_id), ignore_errors=True)
        raise


async def ensure_book(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    data: bytes,
    ext: str,
    filename: str,
    source_uri: str,
    chapters: str | None,
    min_chars: int,
) -> tuple[books.Book, bool] | None:
    """Return ``(book, created)``, creating the book when missing; None when no chapters were found."""

    book_id = books.book_id_for(source_uri)

    async def prepare() -> Prepared | None:
        # Off the event loop and never under the flock, which would stall it.
        prepared = await asyncio.to_thread(_prepare, data, ext, min_chars)
        if prepared is not None and chapters is not None:
            books.validate_selection(chapters, prepared[0].chapters)
        return prepared

    def locked_create(prepared: Prepared | None) -> tuple[books.Book, bool] | None:
        with database.upload_staging_lock(settings.DATA_DIR, book_id):
            book = books.get_book(conn, book_id)
            if book is not None:
                return book, False
            if prepared is None:
                return None
            created = _write_book(
                conn,
                settings,
                data=data,
                ext=ext,
                source_uri=source_uri,
                filename=filename,
                prepared=prepared,
            )
            return created, True

    prepared = None
    if books.get_book(conn, book_id) is None:
        prepared = await prepare()
        if prepared is None:
            return None
    found = locked_create(prepared)
    if found is None:  # deleted since the pre-check: prepare, then retry once under the lock
        if books.get_book(conn, book_id) is None:
            prepared = await prepare()
            if prepared is None:
                return None
        found = locked_create(prepared)
        if found is None:
            raise BookChangedError
    return found
