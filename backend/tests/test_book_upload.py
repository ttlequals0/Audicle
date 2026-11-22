from __future__ import annotations

from pathlib import Path

import pytest
from app.config import get_settings
from app.core import database
from app.services import book_upload, books, file_extraction

from tests.conftest import simple_epub


async def _ensure(env: Path, data: bytes, **overrides):
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        return await book_upload.ensure_book(
            conn,
            get_settings(),
            data=data,
            ext=".epub",
            filename="book.epub",
            source_uri=file_extraction.build_source_uri("h" * 16, "book.epub"),
            chapters=overrides.get("chapters"),
            min_chars=150,
        )
    finally:
        conn.close()


async def test_ensure_book_creates_then_reuses(env: Path) -> None:
    data = simple_epub()
    first = await _ensure(env, data)
    second = await _ensure(env, data)
    assert first is not None and second is not None
    assert (first[1], second[1]) == (True, False)
    assert first[0].id == second[0].id


async def test_ensure_book_bad_selection_writes_nothing(env: Path) -> None:
    with pytest.raises(ValueError, match="unknown chapter"):
        await _ensure(env, simple_epub(), chapters="99")
    assert not (Path(get_settings().DATA_DIR) / "books").exists() or not any(
        (Path(get_settings().DATA_DIR) / "books").iterdir()
    )


async def test_ensure_book_raises_when_book_changes_twice(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed = {"n": 0}

    def _phantom(_conn, book_id):
        # Visible at each pre-check, gone again under the lock.
        claimed["n"] += 1
        if claimed["n"] % 2 == 1:
            return books.Book(
                id=book_id,
                source_uri="x",
                filename="book.epub",
                format="epub",
                title="book",
                author=None,
                season_number=1,
                has_cover=False,
                created_at="",
                voice_id=None,
            )
        return None

    monkeypatch.setattr(books, "get_book", _phantom)
    with pytest.raises(book_upload.BookChangedError):
        await _ensure(env, simple_epub())
    assert claimed["n"] == 4
