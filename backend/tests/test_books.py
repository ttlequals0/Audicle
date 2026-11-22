from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.config import get_settings
from app.core import database
from app.core.paths import book_dir
from app.services import books, jobs
from app.services.book_chapters import DetectedBook, DetectedChapter


@pytest.fixture
def conn(env: Path) -> Iterator[database.sqlite3.Connection]:
    database.run_migrations(env)
    c = database.connect(database.db_path(env))
    try:
        yield c
    finally:
        c.close()


def _detected(n: int = 3, *, short: tuple[int, ...] = ()) -> DetectedBook:
    return DetectedBook(
        format="epub",
        title="Book",
        author="Author",
        cover=None,
        chapters=[
            DetectedChapter(
                idx=i,
                title=f"Chapter {i}",
                locator={"start": i},
                word_count=1000,
                char_count=5000,
                default_selected=i != 1,
                selectable=i not in short,
            )
            for i in range(1, n + 1)
        ],
    )


def _make(conn, name: str = "a", **kw) -> books.Book:
    return books.create_book(
        conn,
        source_uri=f"upload://{name}/{name}.epub",
        filename=f"{name}.epub",
        detected=_detected(**kw),
        has_cover=False,
    )


def _job(conn, book: books.Book, idx: int, status: str) -> None:
    uri = books.chapter_uri(book.source_uri, idx)
    conn.execute(
        "INSERT INTO jobs (id, url, episode_id, status) VALUES (?, ?, ?, ?)",
        (f"j-{book.id}-{idx}-{status}", uri, jobs.compute_episode_id(uri), status),
    )


def _episode(conn, book: books.Book, idx: int) -> None:
    uri = books.chapter_uri(book.source_uri, idx)
    conn.execute(
        "INSERT INTO episodes (id, original_url) VALUES (?, ?)", (jobs.compute_episode_id(uri), uri)
    )


def test_chapter_uri_round_trip() -> None:
    uri = "upload://abc/My%20Book.epub"
    assert books.split_chapter_uri(books.chapter_uri(uri, 7)) == (uri, 7)
    assert books.split_chapter_uri(uri) == (uri, None)


def test_create_book_allocates_increasing_seasons(conn, env: Path) -> None:
    first, second = _make(conn, "a"), _make(conn, "b")
    assert (first.season_number, second.season_number) == (1, 2)
    books.delete_book(conn, get_settings(), second.id)
    assert _make(conn, "c").season_number == 3


@pytest.mark.parametrize("title", [None, "   "])
def test_create_book_falls_back_to_filename_stem(conn, title) -> None:
    detected = _detected()
    detected = DetectedBook(
        format=detected.format,
        title=title,
        author=detected.author,
        cover=None,
        chapters=detected.chapters,
    )
    book = books.create_book(
        conn,
        source_uri="upload://h/My Novel.epub",
        filename="My Novel.epub",
        detected=detected,
        has_cover=False,
    )
    assert book.title == "My Novel"
    assert books.get_book(conn, book.id).title == "My Novel"


def test_chapter_episode_ids_are_deterministic_and_distinct(conn) -> None:
    book = _make(conn, n=3)
    ids = [c.episode_id for c in books.get_chapters(conn, book.id)]
    assert len(set(ids)) == 3
    assert ids == [
        jobs.compute_episode_id(books.chapter_uri(book.source_uri, i)) for i in (1, 2, 3)
    ]
    assert books.chapter_for_episode(conn, ids[1])[1].idx == 2
    assert books.chapter_for_episode(conn, "nope") is None
    assert books.get_chapters(conn, book.id)[0].locator == {"start": 1}


def _queue(conn, book, idxs, voice_id):
    return books.queue_chapters(
        conn,
        book,
        idxs,
        voice_id,
        books.get_chapters(conn, book.id),
        books.chapter_statuses(conn, book.id),
    )


def test_status_derivation(conn) -> None:
    book = _make(conn, n=8)
    _job(conn, book, 2, "queued")
    _job(conn, book, 3, "processing")
    _episode(conn, book, 4)
    _job(conn, book, 5, "failed")
    _job(conn, book, 6, "cancelled")
    books.mark_completed(conn, books.get_chapters(conn, book.id)[6].episode_id)
    _job(conn, book, 8, "failed")
    _job(conn, book, 8, "done")
    assert books.chapter_statuses(conn, book.id) == {
        1: "not_queued",
        2: "queued",
        3: "processing",
        4: "done",
        5: "failed",
        6: "cancelled",
        7: "removed",
        8: "not_queued",
    }


def test_has_published_chapter_only_counts_this_books_episodes(conn) -> None:
    book, other = _make(conn, "a", n=2), _make(conn, "b", n=2)
    assert not books.has_published_chapter(conn, book.id)
    _episode(conn, other, 1)
    assert not books.has_published_chapter(conn, book.id)
    _episode(conn, book, 2)
    assert books.has_published_chapter(conn, book.id)


def test_parse_selection_forms(conn) -> None:
    book = _make(conn, n=10, short=(4,))
    chapters = books.get_chapters(conn, book.id)
    statuses = books.chapter_statuses(conn, book.id)

    def parse(v):
        return books.parse_selection(v, chapters, statuses)

    assert parse("all") == [1, 2, 3, 5, 6, 7, 8, 9, 10]
    assert parse("default") == [c.idx for c in chapters if c.default_selected]
    assert parse("1,3,5-9") == [1, 3, 5, 6, 7, 8, 9]
    assert parse([2, 2, 1]) == [1, 2]
    for bad in ("0", "9-5", "x", "11", "4", [4], [99]):
        with pytest.raises(ValueError):
            parse(bad)


def test_remaining_excludes_completed_and_in_flight(conn) -> None:
    book = _make(conn, n=8)
    _queue(conn, book, [1, 2, 3, 4, 5, 6, 7], None)
    conn.execute("DELETE FROM jobs")
    eps = {c.idx: c.episode_id for c in books.get_chapters(conn, book.id)}
    _job(conn, book, 1, "failed")
    _job(conn, book, 2, "cancelled")
    # 3 stays never-run
    _episode(conn, book, 4)
    books.mark_completed(conn, eps[4])
    books.mark_completed(conn, eps[5])  # removed: completed, episode gone
    _job(conn, book, 6, "queued")
    _episode(conn, book, 7)  # done with completed_at NULL
    conn.execute("UPDATE book_chapters SET requested = 1 WHERE idx = 8")
    conn.execute("UPDATE book_chapters SET selectable = 0 WHERE idx = 8")
    chapters = books.get_chapters(conn, book.id)
    statuses = books.chapter_statuses(conn, book.id)
    assert books.parse_selection("remaining", chapters, statuses) == [1, 2, 3]


def test_queue_chapters_skips_done_and_in_flight_and_sets_requested(conn) -> None:
    book = _make(conn, n=5)
    _episode(conn, book, 1)
    _job(conn, book, 2, "queued")
    _job(conn, book, 3, "processing")
    _job(conn, book, 4, "failed")
    result = _queue(conn, book, [5, 4, 3, 2, 1], "2")
    assert result.skipped == [1, 2, 3]
    assert [j.url for j in result.queued] == [
        books.chapter_uri(book.source_uri, 4),
        books.chapter_uri(book.source_uri, 5),
    ]
    assert all(j.voice_id == "2" and j.status == "queued" for j in result.queued)
    requested = {c.idx: c.requested for c in books.get_chapters(conn, book.id)}
    assert requested == {1: False, 2: False, 3: False, 4: True, 5: True}
    assert books.has_in_flight(conn, book.id)


def test_mark_completed_first_publish_wins_and_ignores_plain_episodes(conn) -> None:
    book = _make(conn, n=1)
    ep = books.get_chapters(conn, book.id)[0].episode_id
    books.mark_completed(conn, ep)
    first = books.get_chapters(conn, book.id)[0].completed_at
    conn.execute("UPDATE book_chapters SET completed_at = '2000-01-01T00:00:00Z'")
    books.mark_completed(conn, ep)
    assert books.get_chapters(conn, book.id)[0].completed_at == "2000-01-01T00:00:00Z"
    assert first is not None
    books.mark_completed(conn, "unrelated")


def test_set_voice(conn) -> None:
    book = _make(conn, n=1)
    books.set_voice(conn, book.id, "3")
    assert books.get_book(conn, book.id).voice_id == "3"


def test_delete_book_removes_rows_and_directory_but_keeps_episodes(conn) -> None:
    settings = get_settings()
    book = _make(conn, n=2)
    _episode(conn, book, 1)
    directory = book_dir(settings, book.id)
    directory.mkdir(parents=True)
    (directory / "source.epub").write_bytes(b"x")
    books.delete_book(conn, settings, book.id)
    assert books.get_book(conn, book.id) is None
    assert books.get_chapters(conn, book.id) == []
    assert not directory.exists()
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1
    books.delete_book(conn, settings, book.id)  # missing directory is tolerated


def test_parse_selection_rejects_huge_range_without_expanding(conn) -> None:
    book = _make(conn, n=3)
    chapters = books.get_chapters(conn, book.id)
    start = time.monotonic()
    with pytest.raises(ValueError, match="1-99999999999"):
        books.parse_selection("1-99999999999", chapters, {})
    assert time.monotonic() - start < 0.5


def test_newest_job_query_uses_episode_index(conn) -> None:
    plan = " ".join(
        row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + books.NEWEST_JOB_SQL, ("b",))
    )
    assert "idx_jobs_episode_id" in plan
    assert "SCAN jobs" not in plan


def test_queue_chapters_skips_unknown_and_unselectable(conn) -> None:
    book = _make(conn, n=3, short=(2,))
    result = _queue(conn, book, [1, 2, 9], None)
    assert [j.url for j in result.queued] == [books.chapter_uri(book.source_uri, 1)]
    assert result.skipped == [2, 9]
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_queue_chapters_marks_requested_before_job_creation(
    conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = _make(conn, n=2)

    def boom(*args, **kwargs):
        raise RuntimeError("crash")

    monkeypatch.setattr(jobs, "create_job", boom)
    with pytest.raises(RuntimeError):
        _queue(conn, book, [1, 2], None)
    assert {c.idx: c.requested for c in books.get_chapters(conn, book.id)} == {1: True, 2: False}


def test_validate_selection_checks_detected_chapters_before_rows_exist() -> None:
    detected = _detected(3, short=(2,)).chapters
    books.validate_selection("all", detected)
    books.validate_selection("default", detected)
    books.validate_selection("1,3", detected)
    books.validate_selection([1, 3], detected)
    for bad in ("99", "0", "3-1", "x", "2", [2], [99], ["1"], [True]):
        with pytest.raises(ValueError):
            books.validate_selection(bad, detected)
