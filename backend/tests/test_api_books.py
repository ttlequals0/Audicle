from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from app.api.v1 import books as books_api
from app.config import get_settings
from app.core import database
from app.main import create_app
from app.services import books, jobs, runtime_settings, voices
from fastapi.testclient import TestClient
from PIL import Image

from tests.conftest import simple_epub


def _png(side: int = 8) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (side, side), (200, 10, 10, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def _split(client: TestClient, data: bytes | None = None, **form: str):
    data = data if data is not None else simple_epub()
    return client.post(
        "/api/v1/upload",
        files={"file": ("book.epub", data, "application/epub+zip")},
        data={"split_chapters": "true", **form},
    )


def _db(env: Path):
    return database.connect(database.db_path(env))


def _set_job(env: Path, episode_id: str, status: str) -> None:
    conn = _db(env)
    try:
        conn.execute("UPDATE jobs SET status = ? WHERE episode_id = ?", (status, episode_id))
    finally:
        conn.close()


def _chapter_episode(env: Path, book_id: str, idx: int) -> str:
    conn = _db(env)
    try:
        return books.get_chapters(conn, book_id)[idx - 1].episode_id
    finally:
        conn.close()


def _publish(env: Path, episode_id: str) -> None:
    conn = _db(env)
    try:
        conn.execute(
            "INSERT INTO episodes (id, original_url) VALUES (?, ?)", (episode_id, f"x-{episode_id}")
        )
        books.mark_completed(conn, episode_id)
    finally:
        conn.close()


def _statuses(body: dict) -> dict[int, str]:
    return {c["index"]: c["status"] for c in body["chapters"]}


@pytest.fixture
def three_voices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = tmp_path / "three_voices"
    d.mkdir()
    for n in (1, 2, 3):
        (d / f"slot{n}.wav").write_bytes(b"RIFFFAKEWAVE")
    monkeypatch.setattr(voices, "voices_dir", lambda: d)


def test_list_books_reports_counts(env: Path) -> None:
    with _client(env) as client:
        book_id = _split(client, chapters="1-2").json()["book_id"]
        rows = client.get("/api/v1/books").json()
    assert [r["book_id"] for r in rows] == [book_id]
    assert rows[0]["chapters"] == []
    assert rows[0]["counts"] == {
        "not_queued": 1,
        "queued": 2,
        "processing": 0,
        "done": 0,
        "failed": 0,
        "cancelled": 0,
        "removed": 0,
    }
    assert rows[0]["can_resume"] is False
    _set_job(env, _chapter_episode(env, book_id, 1), "failed")
    with _client(env) as client:
        assert client.get("/api/v1/books").json()[0]["can_resume"] is True


def test_list_summaries_match_each_book_detail(env: Path) -> None:
    with _client(env) as client:
        ids = [
            _split(client, simple_epub(title=f"Book {i}"), chapters="1-2").json()["book_id"]
            for i in range(3)
        ]
    _set_job(env, _chapter_episode(env, ids[0], 1), "failed")
    _publish(env, _chapter_episode(env, ids[1], 2))
    _set_job(env, _chapter_episode(env, ids[1], 1), "processing")
    with _client(env) as client:
        rows = {r["book_id"]: r for r in client.get("/api/v1/books").json()}
        for book_id in ids:
            detail = client.get(f"/api/v1/books/{book_id}").json()
            assert rows[book_id]["counts"] == detail["counts"]
            assert rows[book_id]["can_resume"] == detail["can_resume"]
    assert rows[ids[0]]["can_resume"] is True
    assert rows[ids[1]]["can_resume"] is False


def test_list_and_detail_agree_on_counts_and_can_resume(env: Path) -> None:
    with _client(env) as client:
        body = _split(client, simple_epub(8), chapters="1-7").json()
        book_id = body["book_id"]
        ep = {c["index"]: c["episode_id"] for c in body["chapters"]}
        _set_job(env, ep[2], "processing")
        _publish(env, ep[3])
        _set_job(env, ep[3], "done")
        _set_job(env, ep[4], "failed")
        _set_job(env, ep[5], "cancelled")
        _publish(env, ep[6])
        _set_job(env, ep[6], "done")
        assert client.delete(f"/api/v1/episodes/{ep[6]}").status_code == 200

        def both() -> tuple[dict, dict]:
            item = next(r for r in client.get("/api/v1/books").json() if r["book_id"] == book_id)
            return item, client.get(f"/api/v1/books/{book_id}").json()

        item, detail = both()
        assert item["counts"] == detail["counts"]
        assert item["can_resume"] == detail["can_resume"]
        assert sum(detail["counts"].values()) == len(detail["chapters"]) == 8
        assert sum(1 for n in detail["counts"].values() if n) >= 4
        assert detail["counts"]["removed"] == 1
        _publish(env, ep[4])
        _set_job(env, ep[4], "done")
        item, detail = both()
        assert item["counts"] == detail["counts"]
        assert item["can_resume"] == detail["can_resume"]
        assert detail["counts"]["done"] == 2


def test_list_books_statement_count_does_not_grow_with_books(env: Path) -> None:
    def statements() -> int:
        seen: list[str] = []
        conn = _db(env)
        conn.set_trace_callback(seen.append)
        try:
            asyncio.run(books_api.list_books(conn))
        finally:
            conn.close()
        return len(seen)

    with _client(env) as client:
        _split(client, simple_epub(title="Book 0"), chapters="1")
        one = statements()
        for i in range(1, 5):
            _split(client, simple_epub(title=f"Book {i}"), chapters="1")
        five = statements()
    assert one == five


def test_get_book_reports_chapter_status(env: Path) -> None:
    with _client(env) as client:
        book_id = _split(client, chapters="1").json()["book_id"]
        body = client.get(f"/api/v1/books/{book_id}").json()
    assert body["title"] == "Test Book"
    assert body["author"] == "Test Author"
    assert body["season_number"] == 1
    assert _statuses(body) == {1: "queued", 2: "not_queued", 3: "not_queued"}
    first = body["chapters"][0]
    assert first["requested"] is True
    assert first["selectable"] is True
    assert first["word_count"] > 0
    assert len(first["episode_id"]) > 0


def test_cover_is_reencoded_as_jpeg(env: Path) -> None:
    with _client(env) as client:
        body = _split(client, simple_epub(cover=_png())).json()
    assert body["has_cover"] is True
    cover = books.cover_file(get_settings(), body["book_id"])
    assert Image.open(cover).format == "JPEG"


def test_unreadable_cover_is_dropped(env: Path) -> None:
    with _client(env) as client:
        response = _split(client, simple_epub(cover=b"not an image"))
    assert response.status_code == 201
    assert response.json()["has_cover"] is False
    assert not books.cover_file(get_settings(), response.json()["book_id"]).exists()


@pytest.mark.parametrize("fmt", ["BMP", "TIFF"])
def test_split_upload_drops_cover_in_unsupported_format(env: Path, fmt: str) -> None:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 10, 10)).save(buf, format=fmt)
    with _client(env) as client:
        response = _split(client, simple_epub(cover=buf.getvalue()))
    assert response.status_code == 201
    assert response.json()["has_cover"] is False
    assert not books.cover_file(get_settings(), response.json()["book_id"]).exists()


def test_queue_remaining_requeues_failed_only(env: Path) -> None:
    with _client(env) as client:
        body = _split(client, chapters="1-3").json()
        book_id = body["book_id"]
        by_idx = {c["index"]: c["episode_id"] for c in body["chapters"]}
        _set_job(env, by_idx[1], "failed")
        _publish(env, by_idx[2])
        _set_job(env, by_idx[2], "done")
        r = client.post(f"/api/v1/books/{book_id}/chapters", json={"chapters": "remaining"})
    assert r.status_code == 201
    # chapter 3 is still queued from the first call, so only the failed chapter is new
    assert r.json()["queued"] == [1]
    assert r.json()["skipped"] == []


def test_queue_remaining_ignores_removed_chapter(env: Path) -> None:
    with _client(env) as client:
        body = _split(client, chapters="1-2").json()
        book_id = body["book_id"]
        by_idx = {c["index"]: c["episode_id"] for c in body["chapters"]}
        for idx in (1, 2):
            _publish(env, by_idx[idx])
            _set_job(env, by_idx[idx], "done")
        assert client.delete(f"/api/v1/episodes/{by_idx[2]}").status_code == 200
        detail = client.get(f"/api/v1/books/{book_id}").json()
        assert _statuses(detail)[2] == "removed"
        assert detail["can_resume"] is False
        r = client.post(f"/api/v1/books/{book_id}/chapters", json={"chapters": "remaining"})
    assert r.status_code == 201
    assert r.json()["queued"] == []


def test_queue_skips_done_and_in_flight(env: Path) -> None:
    with _client(env) as client:
        body = _split(client, chapters="1-2").json()
        book_id = body["book_id"]
        by_idx = {c["index"]: c["episode_id"] for c in body["chapters"]}
        _publish(env, by_idx[1])
        _set_job(env, by_idx[1], "done")
        r = client.post(f"/api/v1/books/{book_id}/chapters", json={"chapters": [1, 2, 3]})
    assert r.status_code == 201
    assert r.json()["skipped"] == [1, 2]
    assert r.json()["queued"] == [3]


def test_queue_requires_voice(env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(env) as client:
        book_id = _split(client).json()["book_id"]
        empty = tmp_path / "empty_voices"
        empty.mkdir()
        monkeypatch.setattr(voices, "voices_dir", lambda: empty)
        r = client.post(f"/api/v1/books/{book_id}/chapters", json={"chapters": "all"})
    assert r.status_code == 400


def test_queue_rejects_bad_bodies(env: Path) -> None:
    with _client(env) as client:
        book_id = _split(client).json()["book_id"]
        url = f"/api/v1/books/{book_id}/chapters"
        assert client.post(url, json={"chapters": "99"}).status_code == 400
        assert client.post(url, json={"chapters": [99]}).status_code == 400
        assert client.post(url, json={"chapters": ["1"]}).status_code == 400
        assert client.post(url, json={"chapters": "all", "extra": 1}).status_code == 400


def test_book_response_exposes_voice(env: Path, three_voices: None) -> None:
    with _client(env) as client:
        book_id = _split(client).json()["book_id"]
        before = client.get(f"/api/v1/books/{book_id}").json()
        url = f"/api/v1/books/{book_id}/chapters"
        assert client.post(url, json={"chapters": [1], "voice": "2"}).status_code == 201
        after = client.get(f"/api/v1/books/{book_id}").json()
    assert before["voice_id"] is None
    assert after["voice_id"] == "2"


def test_queue_response_reports_stored_voice(env: Path, three_voices: None) -> None:
    with _client(env) as client:
        book_id = _split(client).json()["book_id"]
        queued = client.post(
            f"/api/v1/books/{book_id}/chapters", json={"chapters": [1], "voice": "2"}
        )
        assert queued.status_code == 201
        assert queued.json()["voice_id"] == "2"
        split = _split(client, simple_epub(title="Other"), chapters="1", voice="2")
        assert split.status_code == 201
        assert split.json()["voice_id"] == "2"


def test_book_voice_is_sticky(env: Path, three_voices: None) -> None:
    with _client(env) as client:
        book_id = _split(client).json()["book_id"]
        url = f"/api/v1/books/{book_id}/chapters"
        assert client.post(url, json={"chapters": [1], "voice": "2"}).status_code == 201
        assert client.post(url, json={"chapters": [2]}).status_code == 201
        conn = _db(env)
        try:
            voice_ids = {
                j.voice_id
                for ch in books.get_chapters(conn, book_id)[:2]
                for j in [jobs.get_job_by_episode_id(conn, ch.episode_id)]
            }
            assert voice_ids == {"2"}
            assert books.get_book(conn, book_id).voice_id == "2"
        finally:
            conn.close()
        assert client.post(url, json={"chapters": [3], "voice": "3"}).status_code == 201
        conn = _db(env)
        try:
            assert books.get_book(conn, book_id).voice_id == "3"
        finally:
            conn.close()


def test_delete_book_conflicts_while_in_flight(env: Path) -> None:
    with _client(env) as client:
        book_id = _split(client, chapters="1").json()["book_id"]
        assert client.delete(f"/api/v1/books/{book_id}").status_code == 409
        assert client.get(f"/api/v1/books/{book_id}").status_code == 200


def test_delete_book_keeps_published_episodes(env: Path) -> None:
    with _client(env) as client:
        body = _split(client, chapters="1").json()
        book_id = body["book_id"]
        episode_id = body["chapters"][0]["episode_id"]
        _publish(env, episode_id)
        _set_job(env, episode_id, "done")
        revision = _revision(env)
        assert client.delete(f"/api/v1/books/{book_id}").status_code == 204
        assert client.get(f"/api/v1/books/{book_id}").status_code == 404
    assert not (Path(get_settings().DATA_DIR) / "books" / book_id).exists()
    conn = _db(env)
    try:
        assert conn.execute("SELECT 1 FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    finally:
        conn.close()
    assert _revision(env) > revision


def test_delete_book_without_published_chapters_leaves_feed_revision(env: Path) -> None:
    with _client(env) as client:
        book_id = _split(client).json()["book_id"]
        revision = _revision(env)
        assert client.delete(f"/api/v1/books/{book_id}").status_code == 204
    assert _revision(env) == revision


def _revision(env: Path) -> int:
    conn = _db(env)
    try:
        row = conn.execute("SELECT revision FROM feed_state WHERE id = 1").fetchone()
        return row["revision"] if row else 0
    finally:
        conn.close()


def test_book_routes_404(env: Path) -> None:
    with _client(env) as client:
        for bad in ("nope", "x" * 65, "a.b"):
            assert client.get(f"/api/v1/books/{bad}").status_code == 404
            assert client.delete(f"/api/v1/books/{bad}").status_code == 404
            r = client.post(f"/api/v1/books/{bad}/chapters", json={"chapters": "all"})
            assert r.status_code == 404


def test_book_chapter_out_reports_selectable_unticked_short_chapter(env: Path) -> None:
    with _client(env) as client:
        book_id = _split(client, simple_epub(words=(60, 1, 60))).json()["book_id"]
        chapters = client.get(f"/api/v1/books/{book_id}").json()["chapters"]
    short = chapters[1]
    assert short["selectable"] is True
    assert short["short"] is True
    assert short["default_selected"] is False
    assert chapters[0]["selectable"] is True
    assert chapters[0]["short"] is False


def test_split_upload_uses_overlay_min_extraction_chars(env: Path) -> None:
    database.run_migrations(env)
    conn = _db(env)
    try:
        runtime_settings.set_value(conn, "MIN_EXTRACTION_CHARS", 400)
    finally:
        conn.close()
    with TestClient(create_app()) as client:
        # 60 words is ~300 chars: above the 150 default, below the 400 overlay
        body = _split(client, simple_epub(words=(60, 120, 60))).json()
    assert {c["index"]: c["selectable"] for c in body["chapters"]} == {1: True, 2: True, 3: True}


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_split_upload_drops_oversized_cover(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with _client(env) as client:
        for side in (12, 15):  # 1x-2x the limit (warns), above 2x (raises)
            r = _split(client, simple_epub(cover=_png(side)))
            assert r.status_code == 201, side
            assert r.json()["has_cover"] is False
            assert not books.cover_file(get_settings(), r.json()["book_id"]).exists()
