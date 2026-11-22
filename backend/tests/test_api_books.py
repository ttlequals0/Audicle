from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from app.config import get_settings
from app.core import database
from app.main import create_app
from app.services import books, jobs, voices
from fastapi.testclient import TestClient
from PIL import Image

NCX_TYPE = "application/x-dtbncx+xml"
XHTML = "application/xhtml+xml"


def _epub(n: int = 3, *, cover: bytes | None = None, encrypted: bool = False) -> bytes:
    items = [f'<item id="ncx" href="toc.ncx" media-type="{NCX_TYPE}"/>']
    points = ""
    files: dict[str, bytes | str] = {}
    for i in range(1, n + 1):
        files[f"ch{i}.xhtml"] = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>'
            f"<body><h1>Chapter {i}</h1><p>{'word ' * 60}</p></body></html>"
        )
        items.append(f'<item id="c{i}" href="ch{i}.xhtml" media-type="{XHTML}"/>')
        points += (
            f'<navPoint id="p{i}" playOrder="{i}"><navLabel><text>Chapter {i}</text></navLabel>'
            f'<content src="ch{i}.xhtml"/></navPoint>'
        )
    meta = "<dc:title>Test Book</dc:title><dc:creator>Test Author</dc:creator>"
    if cover:
        files["cover.img"] = cover
        items.append(
            '<item id="cov" href="cover.img" media-type="image/png" properties="cover-image"/>'
        )
        meta += '<meta name="cover" content="cov"/>'
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(1, n + 1))
    files["toc.ncx"] = (
        '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        f"<navMap>{points}</navMap></ncx>"
    )
    files["content.opf"] = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
        f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">{meta}</metadata>'
        f'<manifest>{"".join(items)}</manifest><spine toc="ncx">{spine}</spine></package>'
    )
    if encrypted:
        files["META-INF/encryption.xml"] = (
            '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
            'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData><enc:CipherData>'
            '<enc:CipherReference URI="ch1.xhtml"/></enc:CipherData></enc:EncryptedData></encryption>'
        )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (8, 8), (200, 10, 10, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def _split(client: TestClient, data: bytes | None = None, **form: str):
    data = data if data is not None else _epub()
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
    assert rows[0]["counts"] == {"queued": 2, "not_queued": 1}
    assert rows[0]["can_resume"] is False
    _set_job(env, _chapter_episode(env, book_id, 1), "failed")
    with _client(env) as client:
        assert client.get("/api/v1/books").json()[0]["can_resume"] is True


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
        body = _split(client, _epub(cover=_png())).json()
    assert body["has_cover"] is True
    cover = books.cover_file(get_settings(), body["book_id"])
    assert Image.open(cover).format == "JPEG"


def test_unreadable_cover_is_dropped(env: Path) -> None:
    with _client(env) as client:
        response = _split(client, _epub(cover=b"not an image"))
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
