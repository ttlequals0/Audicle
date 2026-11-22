from __future__ import annotations

import io
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.api.v1 import uploads
from app.config import get_settings
from app.core import database
from app.core.paths import media_dir
from app.main import create_app
from app.services import books, episodes, feed, file_extraction, jobs, runtime_settings, voices
from app.services.book_chapters import DetectedBook, DetectedChapter
from app.services.episodes import Episode
from fastapi.testclient import TestClient


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def _seed_upload_episode(env: Path, *, filename: str, write_source: bool = True) -> str:
    """Insert an upload episode (and optionally its stored original) and return its id."""

    database.run_migrations(env)
    media = media_dir(get_settings())
    media.mkdir(parents=True, exist_ok=True)
    uri = file_extraction.build_source_uri("a" * 16, filename)
    episode_id = jobs.compute_episode_id(uri)
    (media / f"{episode_id}.mp3").write_bytes(b"FAKE")
    if write_source:
        file_extraction.source_path(get_settings(), episode_id, filename).write_bytes(b"DOC")
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id=episode_id,
            job_id=None,
            original_url=uri,
            title=Path(filename).stem,
            author="A",
            audio_path=str(media / f"{episode_id}.mp3"),
            artwork_path=None,
            transcript_vtt="WEBVTT\n",
            duration_secs=10,
            source_type="upload",
            source_filename=filename,
        )
    finally:
        conn.close()
    return episode_id


# --- POST /upload -------------------------------------------------------------


def test_upload_md_creates_job_and_stores_original(env: Path) -> None:
    content = b"# Hi\n\nsome body text"
    with _client(env) as client:
        r = client.post("/api/v1/upload", files={"file": ("notes.md", content, "text/markdown")})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "queued"
    episode_id = body["episode_id"]
    assert (media_dir(get_settings()) / f"{episode_id}.source.md").read_bytes() == content
    conn = database.connect(database.db_path(env))
    try:
        job = jobs.get_job(conn, body["job_id"])
        assert job is not None
        assert job.url.startswith("upload://")
        assert job.episode_id == episode_id
    finally:
        conn.close()


def test_upload_source_survives_sweep_during_staging(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import retention

    original_write = uploads.write_bytes_atomic

    def _write_after_sweep(path: Path, data: bytes) -> None:
        retention.sweep_orphan_media(get_settings())
        original_write(path, data)

    monkeypatch.setattr(uploads, "write_bytes_atomic", _write_after_sweep)
    with _client(env) as client:
        response = client.post(
            "/api/v1/upload", files={"file": ("notes.md", b"# note", "text/markdown")}
        )
    assert response.status_code == 201
    episode_id = response.json()["episode_id"]
    assert (media_dir(get_settings()) / f"{episode_id}.source.md").is_file()


def test_upload_rejects_with_400_when_no_voice_loaded(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mirrors the submit guard: no loaded voice -> 400 before any file work.
    from app.services import voices

    empty = tmp_path / "empty_voices"
    empty.mkdir()
    monkeypatch.setattr(voices, "voices_dir", lambda: empty)
    with _client(env) as client:
        r = client.post("/api/v1/upload", files={"file": ("notes.md", b"# Hi", "text/markdown")})
    assert r.status_code == 400


def test_upload_rejects_unsupported_extension(env: Path) -> None:
    with _client(env) as client:
        r = client.post(
            "/api/v1/upload",
            files={"file": ("malware.exe", b"x" * 20, "application/octet-stream")},
        )
    assert r.status_code == 400


def test_upload_rejects_empty_file(env: Path) -> None:
    with _client(env) as client:
        r = client.post("/api/v1/upload", files={"file": ("empty.md", b"", "text/markdown")})
    assert r.status_code == 400


def test_upload_rejects_oversize(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1 MB cap; a >1 MB payload is rejected (the cap is megabytes now).
    monkeypatch.setenv("UPLOAD_MAX_MB", "1")
    get_settings.cache_clear()
    with _client(env) as client:
        r = client.post(
            "/api/v1/upload", files={"file": ("big.md", b"x" * 1_100_000, "text/markdown")}
        )
    assert r.status_code == 400


def test_upload_respects_runtime_tuned_max_mb(env: Path) -> None:
    # A DB override (the operator-tunable path, not just env) drives the cap.
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        runtime_settings.set_value(conn, "UPLOAD_MAX_MB", 1)
    finally:
        conn.close()
    with _client(env) as client:
        r = client.post(
            "/api/v1/upload", files={"file": ("big.md", b"x" * 1_100_000, "text/markdown")}
        )
    assert r.status_code == 400


def test_upload_duplicate_returns_409(env: Path) -> None:
    payload = {"file": ("a.md", b"identical bytes for both", "text/markdown")}
    with _client(env) as client:
        first = client.post("/api/v1/upload", files=payload)
        assert first.status_code == 201
        second = client.post(
            "/api/v1/upload", files={"file": ("a.md", b"identical bytes for both", "text/markdown")}
        )
    assert second.status_code == 409


def test_upload_episode_list_exposes_source_fields(env: Path) -> None:
    _seed_upload_episode(env, filename="report.pdf")
    with _client(env) as client:
        rows = client.get("/api/v1/episodes").json()
    assert rows[0]["source_type"] == "upload"
    assert rows[0]["source_filename"] == "report.pdf"


# --- POST /upload/{id}/reprocess ---------------------------------------------


def test_reprocess_upload_reenqueues_from_stored_file(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    episode_id = _seed_upload_episode(env, filename="report.pdf")
    original_lock = database.upload_staging_lock
    original_create = jobs.create_job
    lock_held = False

    @contextmanager
    def _tracked_lock(data_dir: Path, locked_episode_id: str, *, blocking: bool = True):
        nonlocal lock_held
        with original_lock(data_dir, locked_episode_id, blocking=blocking):
            lock_held = True
            try:
                yield True
            finally:
                lock_held = False

    def _locked_create(*args, **kwargs):
        assert lock_held
        return original_create(*args, **kwargs)

    monkeypatch.setattr(database, "upload_staging_lock", _tracked_lock)
    monkeypatch.setattr(uploads.jobs, "create_job", _locked_create)
    with _client(env) as client:
        r = client.post(f"/api/v1/upload/{episode_id}/reprocess")
    assert r.status_code == 201
    body = r.json()
    assert body["episode_id"] == episode_id
    assert body["replaced_previous"] is True


def test_reprocess_upload_rejects_unsafe_episode_id_without_lockfile(env: Path) -> None:
    with _client(env) as client:
        response = client.post("/api/v1/upload/unsafe.id/reprocess")
    assert response.status_code == 404
    assert not (env / ".upload-unsafe.id.lock").exists()


def test_reprocess_upload_rejects_with_400_when_no_voice_loaded(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reprocess is a job-creating path, so it carries the same no-voice guard as upload.
    episode_id = _seed_upload_episode(env, filename="report.pdf")
    empty = tmp_path / "empty_voices"
    empty.mkdir()
    monkeypatch.setattr(voices, "voices_dir", lambda: empty)
    with _client(env) as client:
        r = client.post(f"/api/v1/upload/{episode_id}/reprocess")
    assert r.status_code == 400


def test_reprocess_rejects_url_episode(env: Path) -> None:
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id="urlep",
            job_id=None,
            original_url="https://example.test/urlep",
            title="t",
            author="a",
            audio_path="/data/media/urlep.mp3",
            artwork_path=None,
            transcript_vtt=None,
            duration_secs=10,
        )
    finally:
        conn.close()
    with _client(env) as client:
        r = client.post("/api/v1/upload/urlep/reprocess")
    assert r.status_code == 400


def test_reprocess_404_when_episode_missing(env: Path) -> None:
    with _client(env) as client:
        r = client.post("/api/v1/upload/ghost/reprocess")
    assert r.status_code == 404


def test_reprocess_409_when_stored_file_gone(env: Path) -> None:
    episode_id = _seed_upload_episode(env, filename="report.pdf", write_source=False)
    with _client(env) as client:
        r = client.post(f"/api/v1/upload/{episode_id}/reprocess")
    assert r.status_code == 409


# --- RSS rendering ------------------------------------------------------------


def test_feed_render_upload_episode_has_no_synthetic_link(env: Path) -> None:
    ep = Episode(
        id="up123",
        job_id="job1",
        title="My Report",
        author="A",
        original_url="upload://abc123/My%20Report.pdf",
        audio_path="/data/media/up123.mp3",
        artwork_path=None,
        transcript_vtt="WEBVTT\n",
        duration_secs=60,
        pub_date="2026-05-28T18:00:00Z",
        created_at="2026-05-28T18:00:00Z",
        updated_at="2026-05-28T18:00:00Z",
        source_type="upload",
        source_filename="My Report.pdf",
    )
    body = feed.render(
        [ep],
        settings=get_settings(),
        podcast_guid="11111111-2222-3333-4444-555555555555",
        last_build=datetime(2026, 5, 28, 18, 0, 0, tzinfo=UTC),
    )
    assert b"upload://" not in body
    assert b"My Report.pdf" in body


def test_reprocess_chapter_episode_uses_book_source(env: Path) -> None:
    database.run_migrations(env)
    detected = DetectedBook(
        format="epub",
        title="Book",
        author=None,
        cover=None,
        chapters=[
            DetectedChapter(
                idx=1,
                title="One",
                locator={},
                word_count=10,
                char_count=50,
                default_selected=True,
                selectable=True,
            )
        ],
    )
    uri = file_extraction.build_source_uri("a" * 16, "novel.epub")
    chapter_url = books.chapter_uri(uri, 1)
    episode_id = jobs.compute_episode_id(chapter_url)
    conn = database.connect(database.db_path(env))
    try:
        book = books.create_book(
            conn, source_uri=uri, filename="novel.epub", detected=detected, has_cover=False
        )
        episodes.upsert(
            conn,
            id=episode_id,
            job_id=None,
            original_url=chapter_url,
            title="Book: One",
            author="A",
            audio_path=None,
            artwork_path=None,
            transcript_vtt="WEBVTT\n",
            duration_secs=10,
            source_type="upload",
            source_filename="novel.epub",
        )
    finally:
        conn.close()
    path = books.source_file(get_settings(), book.id, ".epub")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"EPUB")
    with _client(env) as client:
        ok = client.post(f"/api/v1/upload/{episode_id}/reprocess")
        path.unlink()
        gone = client.post(f"/api/v1/upload/{episode_id}/reprocess")
    assert ok.status_code == 201
    assert gone.status_code == 409


# --- POST /upload with split_chapters -----------------------------------------


def _epub(n: int = 3, *, encrypted: bool = False) -> bytes:
    items = ['<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>']
    points = ""
    files: dict[str, str] = {}
    for i in range(1, n + 1):
        files[f"ch{i}.xhtml"] = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>'
            f"<body><h1>Chapter {i}</h1><p>{'word ' * 60}</p></body></html>"
        )
        items.append(f'<item id="c{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>')
        points += (
            f'<navPoint id="p{i}" playOrder="{i}"><navLabel><text>Chapter {i}</text></navLabel>'
            f'<content src="ch{i}.xhtml"/></navPoint>'
        )
    files["toc.ncx"] = (
        '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        f"<navMap>{points}</navMap></ncx>"
    )
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(1, n + 1))
    files["content.opf"] = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test Book</dc:title>'
        f'</metadata><manifest>{"".join(items)}</manifest><spine toc="ncx">{spine}</spine></package>'
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


def _pdf_without_outline() -> bytes:
    stream = b"BT /F1 24 Tf 72 720 Td (" + b"hello " * 40 + b") Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size " + str(len(objs) + 1).encode() + b" /Root 1 0 R >>\n"
    out += b"startxref\n" + str(xref_pos).encode() + b"\n%%EOF"
    return bytes(out)


def _post_epub(client: TestClient, data: bytes | None = None, **form: str):
    return client.post(
        "/api/v1/upload",
        files={
            "file": ("book.epub", data if data is not None else _epub(), "application/epub+zip")
        },
        data=form,
    )


def _row_counts(env: Path) -> tuple[int, int, int]:
    conn = database.connect(database.db_path(env))
    try:
        return tuple(  # type: ignore[return-value]
            conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("books", "book_chapters", "jobs")
        )
    finally:
        conn.close()


def test_upload_unsplit_epub_is_one_whole_book_job(env: Path) -> None:
    with _client(env) as client:
        r = _post_epub(client)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "queued"
    assert body["chapters_detected"] is None
    assert _row_counts(env) == (0, 0, 1)
    assert (media_dir(get_settings()) / f"{body['episode_id']}.source.epub").is_file()


def test_upload_split_rejects_non_book_type(env: Path) -> None:
    with _client(env) as client:
        r = client.post(
            "/api/v1/upload",
            files={"file": ("notes.md", b"# Hi\n\nsome body text", "text/markdown")},
            data={"split_chapters": "true"},
        )
    assert r.status_code == 400
    assert _row_counts(env) == (0, 0, 0)


def test_upload_split_pdf_without_outline_falls_back_to_one_job(env: Path) -> None:
    with _client(env) as client:
        r = client.post(
            "/api/v1/upload",
            files={"file": ("doc.pdf", _pdf_without_outline(), "application/pdf")},
            data={"split_chapters": "true"},
        )
    assert r.status_code == 201
    assert r.json()["chapters_detected"] is False
    assert _row_counts(env) == (0, 0, 1)


def test_upload_split_epub_creates_book_and_no_jobs(env: Path) -> None:
    with _client(env) as client:
        r = _post_epub(client, split_chapters="true")
    assert r.status_code == 201
    body = r.json()
    assert len(body["chapters"]) == 3
    assert body["queued"] == []
    assert _row_counts(env) == (1, 3, 0)
    assert (Path(get_settings().DATA_DIR) / "books" / body["book_id"] / "source.epub").is_file()


def test_upload_split_with_chapters_queues_in_order(env: Path) -> None:
    with _client(env) as client:
        r = _post_epub(client, split_chapters="true", chapters="2-3")
    assert r.status_code == 201
    body = r.json()
    assert body["queued"] == [2, 3]
    conn = database.connect(database.db_path(env))
    try:
        rows = conn.execute("SELECT episode_id FROM jobs ORDER BY rowid").fetchall()
        by_idx = {c["index"]: c["episode_id"] for c in body["chapters"]}
        assert [r["episode_id"] for r in rows] == [by_idx[2], by_idx[3]]
    finally:
        conn.close()


def test_upload_split_again_returns_existing_book(env: Path) -> None:
    with _client(env) as client:
        first = _post_epub(client, split_chapters="true")
        again = _post_epub(client, split_chapters="true")
    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json()["book_id"] == first.json()["book_id"]
    assert _row_counts(env) == (1, 3, 0)


def test_upload_split_again_with_chapters_still_queues(env: Path) -> None:
    with _client(env) as client:
        _post_epub(client, split_chapters="true")
        again = _post_epub(client, split_chapters="true", chapters="1")
    assert again.status_code == 200
    assert again.json()["queued"] == [1]
    assert _row_counts(env) == (1, 3, 1)


def test_upload_chapters_without_split_is_400(env: Path) -> None:
    with _client(env) as client:
        r = _post_epub(client, chapters="1")
    assert r.status_code == 400
    assert _row_counts(env) == (0, 0, 0)


def test_upload_split_encrypted_epub_is_400_naming_drm(env: Path) -> None:
    with _client(env) as client:
        r = _post_epub(client, _epub(encrypted=True), split_chapters="true")
    assert r.status_code == 400
    assert "DRM" in r.json()["error"]
    assert _row_counts(env) == (0, 0, 0)


def test_upload_split_unknown_chapter_is_400(env: Path) -> None:
    with _client(env) as client:
        r = _post_epub(client, split_chapters="true", chapters="99")
    assert r.status_code == 400
    assert _row_counts(env) == (0, 0, 0)


def test_split_and_unsplit_uploads_of_same_file_coexist(env: Path) -> None:
    data = _epub()
    with _client(env) as client:
        whole = _post_epub(client, data)
        split = _post_epub(client, data, split_chapters="true", chapters="1")
        assert whole.status_code == 201
        assert split.status_code == 201
        assert split.json()["queued"] == [1]
        assert whole.json()["episode_id"] == split.json()["book_id"]
        whole_again = _post_epub(client, data)
    assert whole_again.status_code == 409
    assert _row_counts(env) == (1, 3, 2)


def test_upload_split_removes_book_directory_when_create_fails(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(books, "create_book", _boom)
    with _client(env) as client, pytest.raises(RuntimeError):
        _post_epub(client, split_chapters="true")
    assert list((Path(get_settings().DATA_DIR) / "books").iterdir()) == []


def test_split_upload_with_bad_chapters_writes_nothing_and_keeps_season(env: Path) -> None:
    data = _epub()
    with _client(env) as client:
        bad = _post_epub(client, data, split_chapters="true", chapters="99")
        assert bad.status_code == 400
        assert _row_counts(env) == (0, 0, 0)
        assert list((Path(get_settings().DATA_DIR) / "books").glob("*")) == []
        good = _post_epub(client, data, split_chapters="true", chapters="1")
    assert good.status_code == 201
    assert good.json()["season_number"] == 1


def test_split_upload_survives_book_deleted_between_check_and_lock(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = books.get_book
    calls = {"n": 0}

    def _flaky(conn, book_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return books.Book(
                id=book_id,
                source_uri="x",
                filename="book.epub",
                format="epub",
                title=None,
                author=None,
                season_number=1,
                has_cover=False,
                created_at="",
                voice_id=None,
            )
        return real(conn, book_id)

    monkeypatch.setattr(uploads.books, "get_book", _flaky)
    with _client(env) as client:
        r = _post_epub(client, split_chapters="true")
    assert r.status_code == 201
    assert len(r.json()["chapters"]) == 3
    assert _row_counts(env) == (1, 3, 0)
