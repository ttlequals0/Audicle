"""Book and chapter rows for chapter-split uploads.

A chapter is an ordinary job whose URL is the book's ``upload://`` source URI plus
``#chapter={idx}``. Chapter status is derived from the job and episode tables, never stored.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections import Counter
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings
from app.core.paths import book_dir
from app.services import feed_revision, jobs, settings_store
from app.services.book_chapters import DetectedBook, DetectedChapter

CHAPTER_FRAGMENT = "#chapter="

SEASON_HIGH_WATER_KEY = "book_season_high_water"
_ONE_BOOK_SCOPE = "SELECT episode_id FROM book_chapters WHERE book_id = ?"
_ALL_BOOKS_SCOPE = "SELECT episode_id FROM book_chapters"


def _newest_job_sql(scope: str) -> str:
    return (
        "SELECT j.episode_id, j.status FROM ("
        "SELECT episode_id, MAX(rowid) AS rid FROM jobs "
        f"WHERE episode_id IN ({scope}) "
        "GROUP BY episode_id) n JOIN jobs j ON j.rowid = n.rid"
    )


NEWEST_JOB_SQL = _newest_job_sql(_ONE_BOOK_SCOPE)
_IN_FLIGHT_SQL = "'staging','queued','processing'"
_KEYWORDS = ("all", "default", "remaining")
_BOOK_COLUMNS = "id, source_uri, filename, format, title, author, season_number, has_cover, created_at, voice_id"
_CHAPTER_COLUMNS = (
    "book_id, idx, title, locator, word_count, char_count, default_selected, "
    "selectable, requested, episode_id, completed_at"
)


@dataclass(frozen=True)
class Book:
    id: str
    source_uri: str
    filename: str
    format: str
    title: str
    author: str | None
    season_number: int
    has_cover: bool
    created_at: str
    voice_id: str | None


@dataclass(frozen=True)
class Chapter:
    book_id: str
    idx: int
    title: str
    locator: dict[str, Any]
    word_count: int
    char_count: int
    default_selected: bool
    selectable: bool
    requested: bool
    episode_id: str
    completed_at: str | None


@dataclass(frozen=True)
class BookSummary:
    counts: dict[str, int]
    can_resume: bool


@dataclass(frozen=True)
class QueueResult:
    queued: list[jobs.Job]
    skipped: list[int]


def _row_to_book(row: sqlite3.Row) -> Book:
    return Book(
        id=row["id"],
        source_uri=row["source_uri"],
        filename=row["filename"],
        format=row["format"],
        title=row["title"],
        author=row["author"],
        season_number=row["season_number"],
        has_cover=bool(row["has_cover"]),
        created_at=row["created_at"],
        voice_id=row["voice_id"],
    )


def _row_to_chapter(row: sqlite3.Row) -> Chapter:
    return Chapter(
        book_id=row["book_id"],
        idx=row["idx"],
        title=row["title"],
        locator=json.loads(row["locator"]),
        word_count=row["word_count"],
        char_count=row["char_count"],
        default_selected=bool(row["default_selected"]),
        selectable=bool(row["selectable"]),
        requested=bool(row["requested"]),
        episode_id=row["episode_id"],
        completed_at=row["completed_at"],
    )


def chapter_uri(source_uri: str, idx: int) -> str:
    return f"{source_uri}{CHAPTER_FRAGMENT}{idx}"


def split_chapter_uri(uri: str) -> tuple[str, int | None]:
    base, sep, tail = uri.rpartition(CHAPTER_FRAGMENT)
    if sep and tail.isascii() and tail.isdigit():
        return base, int(tail)
    return uri, None


def book_id_for(source_uri: str) -> str:
    return jobs.compute_episode_id(source_uri)


def source_file(settings: Settings, book_id: str, ext: str) -> Path:
    return book_dir(settings, book_id) / f"source{ext}"


def cover_file(settings: Settings, book_id: str) -> Path:
    return book_dir(settings, book_id) / "cover.jpg"


def create_book(
    conn: sqlite3.Connection,
    *,
    source_uri: str,
    filename: str,
    detected: DetectedBook,
    has_cover: bool,
) -> Book:
    book_id = book_id_for(source_uri)
    title = (detected.title or "").strip() or Path(filename).stem
    conn.execute("BEGIN IMMEDIATE")
    try:
        # High-water mark in settings so a deleted book's season is never reused.
        mark = settings_store.get(conn, SEASON_HIGH_WATER_KEY)
        live = conn.execute("SELECT COALESCE(MAX(season_number), 0) FROM books").fetchone()[0]
        season = max(live, int(mark) if mark else 0) + 1
        settings_store.set_without_commit(conn, SEASON_HIGH_WATER_KEY, str(season))
        conn.execute(
            "INSERT INTO books (id, source_uri, filename, format, title, author, "
            "season_number, has_cover) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                book_id,
                source_uri,
                filename,
                detected.format,
                title,
                detected.author,
                season,
                int(has_cover),
            ),
        )
        conn.executemany(
            "INSERT INTO book_chapters (book_id, idx, title, locator, word_count, char_count, "
            "selectable, default_selected, episode_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    book_id,
                    ch.idx,
                    ch.title,
                    json.dumps(ch.locator),
                    ch.word_count,
                    ch.char_count,
                    int(ch.selectable),
                    int(ch.default_selected),
                    jobs.compute_episode_id(chapter_uri(source_uri, ch.idx)),
                )
                for ch in detected.chapters
            ],
        )
        # Chapter episodes outlive a deleted book; a re-upload takes them over.
        adopted = conn.execute(
            "UPDATE episodes SET season_number = ?, "
            "episode_number = (SELECT idx FROM book_chapters WHERE episode_id = episodes.id) "
            "WHERE id IN (SELECT episode_id FROM book_chapters WHERE book_id = ?)",
            (season, book_id),
        ).rowcount
        if adopted:
            conn.execute(
                "UPDATE book_chapters SET requested = 1, "
                "completed_at = (SELECT created_at FROM episodes WHERE id = book_chapters.episode_id) "
                "WHERE book_id = ? AND episode_id IN (SELECT id FROM episodes)",
                (book_id,),
            )
            feed_revision.bump(conn)
        conn.execute("COMMIT")
    except Exception:
        with suppress(sqlite3.OperationalError):
            conn.execute("ROLLBACK")
        raise
    book = get_book(conn, book_id)
    if book is None:
        raise RuntimeError(f"book {book_id} missing after insert")
    return book


def get_book(conn: sqlite3.Connection, book_id: str) -> Book | None:
    row = conn.execute(f"SELECT {_BOOK_COLUMNS} FROM books WHERE id = ?", (book_id,)).fetchone()
    return _row_to_book(row) if row else None


def list_books(conn: sqlite3.Connection) -> list[Book]:
    rows = conn.execute(f"SELECT {_BOOK_COLUMNS} FROM books ORDER BY season_number DESC")
    return [_row_to_book(r) for r in rows]


def get_chapters(conn: sqlite3.Connection, book_id: str) -> list[Chapter]:
    rows = conn.execute(
        f"SELECT {_CHAPTER_COLUMNS} FROM book_chapters WHERE book_id = ? ORDER BY idx ASC",
        (book_id,),
    )
    return [_row_to_chapter(r) for r in rows]


def chapter_for_episode(conn: sqlite3.Connection, episode_id: str) -> tuple[Book, Chapter] | None:
    row = conn.execute(
        f"SELECT {_CHAPTER_COLUMNS} FROM book_chapters WHERE episode_id = ?", (episode_id,)
    ).fetchone()
    if row is None:
        return None
    chapter = _row_to_chapter(row)
    book = get_book(conn, chapter.book_id)
    return (book, chapter) if book else None


@dataclass(frozen=True)
class _JobState:
    processing: set[str]
    queued: set[str]
    newest: dict[str, str]
    published: set[str]


def _job_state(conn: sqlite3.Connection, scope: str, params: tuple[str, ...]) -> _JobState:
    processing: set[str] = set()
    queued: set[str] = set()
    for row in conn.execute(
        f"SELECT episode_id, status FROM jobs WHERE status IN ({_IN_FLIGHT_SQL}) "
        f"AND episode_id IN ({scope})",
        params,
    ):
        (processing if row["status"] == "processing" else queued).add(row["episode_id"])
    newest = {
        row["episode_id"]: row["status"] for row in conn.execute(_newest_job_sql(scope), params)
    }
    published = {
        row["id"] for row in conn.execute(f"SELECT id FROM episodes WHERE id IN ({scope})", params)
    }
    return _JobState(processing, queued, newest, published)


def _status(state: _JobState, episode_id: str, completed_at: str | None) -> str:
    if episode_id in state.processing:
        return "processing"
    if episode_id in state.queued:
        return "queued"
    if episode_id in state.published:
        return "done"
    if completed_at:
        return "removed"
    if state.newest.get(episode_id) in ("failed", "cancelled"):
        return state.newest[episode_id]
    return "not_queued"


def chapter_statuses(conn: sqlite3.Connection, book_id: str) -> dict[int, str]:
    chapters = conn.execute(
        "SELECT idx, episode_id, completed_at FROM book_chapters WHERE book_id = ?", (book_id,)
    ).fetchall()
    state = _job_state(conn, _ONE_BOOK_SCOPE, (book_id,))
    return {ch["idx"]: _status(state, ch["episode_id"], ch["completed_at"]) for ch in chapters}


def _is_remaining(
    requested: bool, selectable: bool, completed_at: str | None, status: str | None
) -> bool:
    return (
        requested
        and selectable
        and completed_at is None
        and status not in ("queued", "processing", "done")
    )


def book_summaries(conn: sqlite3.Connection) -> dict[str, BookSummary]:
    """Per-status counts and resumability for every book, in a fixed number of queries."""

    rows = conn.execute(
        "SELECT book_id, idx, selectable, requested, completed_at, episode_id FROM book_chapters"
    ).fetchall()
    state = _job_state(conn, _ALL_BOOKS_SCOPE, ())
    counts: dict[str, Counter[str]] = {}
    resumable: set[str] = set()
    for row in rows:
        status = _status(state, row["episode_id"], row["completed_at"])
        counts.setdefault(row["book_id"], Counter())[status] += 1
        if _is_remaining(
            bool(row["requested"]), bool(row["selectable"]), row["completed_at"], status
        ):
            resumable.add(row["book_id"])
    return {
        book_id: BookSummary(counts=dict(book_counts), can_resume=book_id in resumable)
        for book_id, book_counts in counts.items()
    }


def _parse_index_list(value: str, max_idx: int) -> list[int]:
    idxs: list[int] = []
    for token in value.split(","):
        token = token.strip()
        lo, sep, hi = token.partition("-")
        if not (lo.isascii() and lo.isdigit() and (not sep or (hi.isascii() and hi.isdigit()))):
            raise ValueError(f"invalid chapter selection token: {token!r}")
        start, end = int(lo), int(hi) if sep else int(lo)
        if end < start:
            raise ValueError(f"reversed chapter range: {token!r}")
        if start < 1 or end > max_idx:
            raise ValueError(f"unknown chapter index in {token!r}")
        idxs.extend(range(start, end + 1))
    return idxs


def _explicit_selection(
    value: str | list[int], chapters: Sequence[Chapter] | Sequence[DetectedChapter]
) -> list[int]:
    if isinstance(value, str):
        requested = _parse_index_list(value, max((c.idx for c in chapters), default=0))
    else:
        if not all(isinstance(i, int) and not isinstance(i, bool) for i in value):
            raise ValueError("chapter indexes must be integers")
        requested = list(value)
    by_idx = {c.idx: c for c in chapters}
    for idx in requested:
        chapter = by_idx.get(idx)
        if chapter is None:
            raise ValueError(f"unknown chapter index: {idx}")
        if not chapter.selectable:
            raise ValueError(f"chapter {idx} has no text to narrate")
    return sorted(set(requested))


def _keyword(value: str | list[int]) -> str | None:
    keyword = value.strip().lower() if isinstance(value, str) else None
    return keyword if keyword in _KEYWORDS else None


def validate_selection(
    value: str | list[int], chapters: Sequence[Chapter] | Sequence[DetectedChapter]
) -> None:
    """Raise ValueError for a bad explicit selection; usable before any rows exist."""

    if _keyword(value) is None:
        _explicit_selection(value, chapters)


def parse_selection(
    value: str | list[int], chapters: list[Chapter], statuses: dict[int, str]
) -> list[int]:
    keyword = _keyword(value)
    if keyword == "all":
        return [c.idx for c in chapters if c.selectable]
    if keyword == "default":
        return [c.idx for c in chapters if c.default_selected]
    if keyword == "remaining":
        return [
            c.idx
            for c in chapters
            if _is_remaining(c.requested, c.selectable, c.completed_at, statuses.get(c.idx))
        ]
    return _explicit_selection(value, chapters)


def queue_chapters(
    conn: sqlite3.Connection,
    book: Book,
    idxs: list[int],
    voice_id: str | None,
    chapters: list[Chapter],
    statuses: dict[int, str],
) -> QueueResult:
    selectable = {c.idx for c in chapters if c.selectable}
    queued: list[jobs.Job] = []
    skipped: list[int] = []
    for idx in sorted(set(idxs)):
        if idx not in selectable or statuses[idx] in ("done", "queued", "processing"):
            skipped.append(idx)
            continue
        # Set before the job exists so a crash cannot leave a queued job unrequested.
        conn.execute(
            "UPDATE book_chapters SET requested = 1 WHERE book_id = ? AND idx = ?",
            (book.id, idx),
        )
        try:
            result = jobs.create_job(conn, chapter_uri(book.source_uri, idx), voice_id=voice_id)
        except jobs.DuplicateSubmissionError:
            skipped.append(idx)
            continue
        queued.append(result.job)
    return QueueResult(queued=queued, skipped=skipped)


def mark_completed(conn: sqlite3.Connection, episode_id: str) -> None:
    conn.execute(
        "UPDATE book_chapters SET completed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
        "WHERE episode_id = ? AND completed_at IS NULL",
        (episode_id,),
    )


def set_voice(conn: sqlite3.Connection, book_id: str, voice_id: str | None) -> None:
    conn.execute("UPDATE books SET voice_id = ? WHERE id = ?", (voice_id, book_id))


def has_in_flight(conn: sqlite3.Connection, book_id: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM jobs WHERE status IN ({_IN_FLIGHT_SQL}) AND episode_id IN "
        "(SELECT episode_id FROM book_chapters WHERE book_id = ?) LIMIT 1",
        (book_id,),
    ).fetchone()
    return row is not None


def has_published_chapter(conn: sqlite3.Connection, book_id: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM episodes WHERE id IN ({_ONE_BOOK_SCOPE}) LIMIT 1", (book_id,)
    ).fetchone()
    return row is not None


def delete_book(conn: sqlite3.Connection, settings: Settings, book_id: str) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM book_chapters WHERE book_id = ?", (book_id,))
        conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
        conn.execute("COMMIT")
    except Exception:
        with suppress(sqlite3.OperationalError):
            conn.execute("ROLLBACK")
        raise
    with suppress(FileNotFoundError):
        shutil.rmtree(book_dir(settings, book_id))
