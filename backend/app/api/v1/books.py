"""``/api/v1/books`` -- chapter-split books: list, detail, queue chapters, delete."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, StrictInt

from app.api.deps import get_conn, require_voice_loaded
from app.config import Settings, get_settings
from app.core import database
from app.services import books, feed_revision, voices

router = APIRouter(prefix="/books", tags=["books"])
_BOOK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class BookChapterOut(BaseModel):
    index: int
    title: str
    word_count: int
    default_selected: bool
    selectable: bool
    requested: bool
    status: str
    episode_id: str


class BookResponse(BaseModel):
    book_id: str
    title: str | None
    author: str | None
    filename: str
    format: str
    season_number: int
    has_cover: bool
    created_at: str
    counts: dict[str, int]
    can_resume: bool
    chapters: list[BookChapterOut]
    queued: list[int] = []
    skipped: list[int] = []


class QueueChaptersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapters: str | list[StrictInt]
    voice: str | None = None


def book_response(
    conn: sqlite3.Connection,
    book: books.Book,
    *,
    with_chapters: bool = True,
    queued: list[int] | None = None,
    skipped: list[int] | None = None,
) -> BookResponse:
    chapters = books.get_chapters(conn, book.id)
    statuses = books.chapter_statuses(conn, book.id)
    return BookResponse(
        book_id=book.id,
        title=book.title,
        author=book.author,
        filename=book.filename,
        format=book.format,
        season_number=book.season_number,
        has_cover=book.has_cover,
        created_at=book.created_at,
        counts=dict(Counter(statuses.values())),
        can_resume=bool(books.parse_selection("remaining", chapters, statuses)),
        chapters=[
            BookChapterOut(
                index=c.idx,
                title=c.title,
                word_count=c.word_count,
                default_selected=c.default_selected,
                selectable=c.selectable,
                requested=c.requested,
                status=statuses[c.idx],
                episode_id=c.episode_id,
            )
            for c in chapters
        ]
        if with_chapters
        else [],
        queued=queued or [],
        skipped=skipped or [],
    )


def queue_selection(
    conn: sqlite3.Connection, book: books.Book, selection: str | list[int], voice: str | None
) -> BookResponse:
    """Parse ``selection``, resolve the book's single voice, and queue the chapters.
    Raises ValueError for a bad selection; callers map it to 400."""

    chapters = books.get_chapters(conn, book.id)
    idxs = books.parse_selection(selection, chapters, books.chapter_statuses(conn, book.id))
    if voice is None and book.voice_id:
        voice_id = book.voice_id
    else:
        voice_id = voices.resolve(conn, voice)
        books.set_voice(conn, book.id, voice_id)
    result = books.queue_chapters(conn, book, idxs, voice_id)
    idx_by_episode = {c.episode_id: c.idx for c in chapters}
    queued = sorted(idx_by_episode[j.episode_id] for j in result.queued)
    return book_response(conn, book, queued=queued, skipped=result.skipped)


def _get_or_404(conn: sqlite3.Connection, book_id: str) -> books.Book:
    book = books.get_book(conn, book_id) if _BOOK_ID_RE.fullmatch(book_id) else None
    if book is None:
        raise HTTPException(status_code=404, detail="book not found")
    return book


@router.get("", response_model=list[BookResponse], summary="List chapter-split books")
async def list_books(conn: Annotated[sqlite3.Connection, Depends(get_conn)]) -> list[BookResponse]:
    return [book_response(conn, b, with_chapters=False) for b in books.list_books(conn)]


@router.get("/{book_id}", response_model=BookResponse, summary="Book detail with every chapter")
async def get_book(
    book_id: str, conn: Annotated[sqlite3.Connection, Depends(get_conn)]
) -> BookResponse:
    return book_response(conn, _get_or_404(conn, book_id))


@router.post(
    "/{book_id}/chapters",
    status_code=201,
    response_model=BookResponse,
    summary="Queue chapters of a book for narration",
    dependencies=[Depends(require_voice_loaded)],
)
async def queue_chapters(
    book_id: str,
    body: QueueChaptersRequest,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> BookResponse:
    _get_or_404(conn, book_id)
    with database.upload_staging_lock(settings.DATA_DIR, book_id):
        book = _get_or_404(conn, book_id)
        try:
            return queue_selection(conn, book, body.chapters, body.voice)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{book_id}", status_code=204, summary="Delete a book and its stored source")
async def delete_book(
    book_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    _get_or_404(conn, book_id)
    with database.upload_staging_lock(settings.DATA_DIR, book_id):
        _get_or_404(conn, book_id)
        if books.has_in_flight(conn, book_id):
            raise HTTPException(status_code=409, detail="a chapter of this book is processing")
        published = "done" in books.chapter_statuses(conn, book_id).values()
        books.delete_book(conn, settings, book_id)
        if published:
            feed_revision.bump(conn)
    return Response(status_code=204)
