from __future__ import annotations

import asyncio
import io
from pathlib import Path

import docx
import pytest
from app.config import get_settings
from app.core import database
from app.services import book_chapters, books, file_extraction, jobs, ocr
from app.services.extraction_types import ExtractionPermanentError, ExtractionTooShortError
from PIL import Image, ImageDraw

from tests.conftest import pdf_book, simple_epub, text_page_pdf

_LONG = "Lorem ipsum dolor sit amet consectetur adipiscing elit. " * 20  # ~1100 chars


def _make_docx(*, title: str | None, author: str | None, body: str) -> bytes:
    document = docx.Document()
    if title:
        document.core_properties.title = title
    if author:
        document.core_properties.author = author
    for para in body.split("\n\n"):
        document.add_paragraph(para)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _forbid_docx_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("parser reached")

    monkeypatch.setattr(file_extraction.docx, "Document", boom)


def test_docx_over_xml_cap_is_rejected_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _make_docx(title=None, author=None, body="hello")
    monkeypatch.setattr(file_extraction, "MAX_DOCX_XML_BYTES", 100)
    _forbid_docx_parse(monkeypatch)
    with pytest.raises(ExtractionPermanentError, match="too large once uncompressed"):
        file_extraction._parse_docx(data)


def test_docx_over_total_uncompressed_cap_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _make_docx(title=None, author=None, body="hello")
    monkeypatch.setattr(book_chapters, "MAX_UNCOMPRESSED_BYTES", 100)
    _forbid_docx_parse(monkeypatch)
    with pytest.raises(ExtractionPermanentError, match="too large once uncompressed"):
        file_extraction._parse_docx(data)


def test_docx_with_too_many_entries_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _make_docx(title=None, author=None, body="hello")
    monkeypatch.setattr(book_chapters, "MAX_ARCHIVE_ENTRIES", 1)
    _forbid_docx_parse(monkeypatch)
    with pytest.raises(ExtractionPermanentError, match="too many entries"):
        file_extraction._parse_docx(data)


def test_docx_that_is_not_a_zip_is_a_permanent_error() -> None:
    with pytest.raises(ExtractionPermanentError, match="could not read DOCX"):
        file_extraction._parse_docx(b"not a zip")


def _make_docx_with_table() -> bytes:
    document = docx.Document()
    document.add_paragraph("Before table. " + _LONG)
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Table first cell. " + _LONG
    table.cell(0, 1).text = "Table second cell. " + _LONG
    document.add_paragraph("After table. " + _LONG)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _make_docx_with_merged_nested_table() -> bytes:
    document = docx.Document()
    document.add_paragraph("Before merged table. " + _LONG)
    table = document.add_table(rows=1, cols=2)
    merged = table.cell(0, 0).merge(table.cell(0, 1))
    merged.text = "Merged content. " + _LONG
    nested = merged.add_table(rows=1, cols=1)
    nested.cell(0, 0).text = "Nested content. " + _LONG
    document.add_paragraph("After merged table. " + _LONG)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _run(env: Path, filename: str, data: bytes):
    """Store ``data`` as the upload for a fresh job and run extract_file."""

    settings = get_settings()
    uri = file_extraction.build_source_uri("deadbeef" * 8, filename)
    episode_id = jobs.compute_episode_id(uri)
    path = file_extraction.source_path(settings, episode_id, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    job = jobs.Job(
        id="job1",
        url=uri,
        episode_id=episode_id,
        status="processing",
        stage="extract",
        error=None,
        created_at="t",
        updated_at="t",
    )
    return file_extraction.extract_file(job, settings)


# --- URI / filename helpers ---------------------------------------------------


def test_source_uri_round_trips_filename_with_spaces() -> None:
    uri = file_extraction.build_source_uri("abc123", "My Report (final).pdf")
    assert file_extraction.is_upload_source(uri)
    content_hash, filename = file_extraction.parse_source_uri(uri)
    assert content_hash == "abc123"
    assert filename == "My Report (final).pdf"


def test_sanitize_filename_strips_path_and_control_chars() -> None:
    assert file_extraction.sanitize_filename("../../etc/passwd") == "passwd"
    assert file_extraction.sanitize_filename("a\x00b\x1f.md") == "ab.md"
    assert file_extraction.sanitize_filename("C:\\docs\\x.docx") == "x.docx"


def test_extension_of_is_lowercased() -> None:
    assert file_extraction.extension_of("Paper.PDF") == ".pdf"


# --- per-format extraction ----------------------------------------------------


async def test_extract_markdown_uses_h1_as_title(env: Path) -> None:
    md = f"# The Real Title\n\n{_LONG}"
    result = await _run(env, "notes.md", md.encode())
    assert result.metadata["title"] == "The Real Title"
    assert "Lorem ipsum" in result.markdown


async def test_extract_text_falls_back_to_filename_title(env: Path) -> None:
    result = await _run(env, "My Article.txt", _LONG.encode())
    assert result.metadata["title"] == "My Article"


async def test_extract_pdf_text_and_filename_title(env: Path) -> None:
    result = await _run(env, "whitepaper.pdf", text_page_pdf(_LONG))
    assert "Lorem ipsum" in result.markdown
    assert result.metadata["title"] == "whitepaper"


async def test_extract_docx_uses_core_property_title_and_author(env: Path) -> None:
    data = _make_docx(title="Doc Title", author="Jane Doe", body=_LONG + "\n\n" + _LONG)
    result = await _run(env, "report.docx", data)
    assert result.metadata["title"] == "Doc Title"
    assert result.metadata["author"] == "Jane Doe"
    assert "Lorem ipsum" in result.markdown


async def test_extract_docx_includes_tables_in_document_order(env: Path) -> None:
    result = await _run(env, "report.docx", _make_docx_with_table())
    assert result.markdown.index("Before table") < result.markdown.index("Table first cell")
    assert result.markdown.index("Table second cell") < result.markdown.index("After table")


async def test_extract_docx_deduplicates_merged_cells_and_keeps_nested_tables(env: Path) -> None:
    result = await _run(env, "report.docx", _make_docx_with_merged_nested_table())
    assert result.markdown.count("Merged content") == 1
    assert result.markdown.index("Merged content") < result.markdown.index("Nested content")
    assert result.markdown.index("Nested content") < result.markdown.index("After merged table")


def test_extract_mixed_pdf_ocr_recovers_sparse_page_in_order(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = "Native text from page one. " * 40
    monkeypatch.setattr(
        file_extraction, "_parse_pdf", lambda _data, _first, _last: ([native, ""], {1}, {})
    )
    seen: dict[str, list[int]] = {}

    def _fake_ocr(_data, _settings, _beat, page_indices):
        seen["indices"] = page_indices
        return {1: "Scanned text from page two."}

    monkeypatch.setattr(ocr, "ocr_pdf_pages", _fake_ocr)
    markdown, _ = file_extraction._parse(b"pdf", ".pdf", get_settings(), lambda: None)
    assert seen["indices"] == [1]
    assert markdown.index("Native text") < markdown.index("Scanned text")


def test_mixed_pdf_ocr_failure_fails_extraction_instead_of_publishing_partial(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = "Native page text. " * 40
    monkeypatch.setattr(
        file_extraction, "_parse_pdf", lambda _data, _first, _last: ([native, ""], {1}, {})
    )

    def _fail(*_args):
        raise ocr.OcrLowConfidenceError("OCR found no readable text on PDF page 2")

    monkeypatch.setattr(ocr, "ocr_pdf_pages", _fail)
    with pytest.raises(ExtractionPermanentError, match="Could not read scanned PDF pages"):
        file_extraction._parse(b"pdf", ".pdf", get_settings(), lambda: None)


def test_blank_pdf_page_is_not_sent_to_ocr(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        file_extraction, "_parse_pdf", lambda _data, _first, _last: ([""], set(), {})
    )

    def _fail(*_args):
        raise AssertionError("a blank page must not trigger OCR")

    monkeypatch.setattr(ocr, "ocr_pdf_pages", _fail)
    assert file_extraction._parse(b"pdf", ".pdf", get_settings(), lambda: None)[0] == ""


def test_pdf_image_detection_distinguishes_blank_pages_and_nested_scans() -> None:
    blank = {"/Resources": {"/XObject": {}}}
    scan = {"/Resources": {"/XObject": {"/image": {"/Subtype": "/Image"}}}}
    nested_scan = {
        "/Resources": {
            "/XObject": {
                "/form": {
                    "/Subtype": "/Form",
                    "/Resources": {"/XObject": {"/image": {"/Subtype": "/Image"}}},
                }
            }
        }
    }
    assert not book_chapters.page_has_image(blank)
    assert book_chapters.page_has_image(scan)
    assert book_chapters.page_has_image(nested_scan)


async def test_extract_html_pulls_main_article(env: Path) -> None:
    html = (
        "<html><head><title>Page Title</title></head><body>"
        "<nav>menu junk</nav>"
        f"<article><h1>Headline</h1><p>{_LONG}</p></article>"
        "<footer>footer junk</footer></body></html>"
    )
    result = await _run(env, "saved.html", html.encode())
    assert "Lorem ipsum" in result.markdown
    assert "menu junk" not in result.markdown


# --- failure modes ------------------------------------------------------------


async def test_extract_too_short_raises(env: Path) -> None:
    with pytest.raises(ExtractionTooShortError):
        await _run(env, "tiny.txt", b"hello")


async def test_extract_missing_file_raises_permanent(env: Path) -> None:
    settings = get_settings()
    uri = file_extraction.build_source_uri("nope", "gone.pdf")
    job = jobs.Job(
        id="j",
        url=uri,
        episode_id=jobs.compute_episode_id(uri),
        status="processing",
        stage="extract",
        error=None,
        created_at="t",
        updated_at="t",
    )
    with pytest.raises(ExtractionPermanentError):
        await file_extraction.extract_file(job, settings)


# --- OCR fallback (section 4) -----------------------------------------------


def _text_png(lines: list[str], size=(1000, 400)) -> bytes:
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((40, 60 + 80 * i), line, fill="black", font_size=40)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def test_extract_png_upload_runs_ocr(env: Path) -> None:
    """A PNG upload is routed straight to OCR and produces article text."""

    lines = [
        "The quick brown fox jumps over the lazy dog and keeps going.",
        "Audicle converts articles into narrated podcast episodes daily.",
    ] * 5
    result = await _run(env, "scan.png", _text_png(lines, size=(1000, 900)))
    assert "quick brown fox" in result.markdown
    assert result.metadata["title"] == "scan"


async def test_extract_image_rejected_when_ocr_disabled(env: Path, monkeypatch) -> None:
    monkeypatch.setenv("OCR_ENABLED", "false")
    get_settings.cache_clear()
    try:
        with pytest.raises(ExtractionPermanentError):
            await _run(env, "scan.png", _text_png(["hello there"]))
    finally:
        get_settings.cache_clear()


async def test_extract_scanned_pdf_falls_back_to_ocr(env: Path, monkeypatch) -> None:
    """A scanned page with little native text goes through OCR."""

    called = {}

    def _fake_ocr_pdf_pages(data, settings, beat, page_indices):
        called["n"] = True
        called["pages"] = page_indices
        beat()
        return {0: "Recovered by OCR. " * 40}

    monkeypatch.setattr(
        file_extraction, "_parse_pdf", lambda _data, _first, _last: (["tiny"], {0}, {})
    )
    monkeypatch.setattr(ocr, "ocr_pdf_pages", _fake_ocr_pdf_pages)
    result = await _run(env, "scan.pdf", text_page_pdf("tiny"))
    assert called.get("n")
    assert called["pages"] == [0]
    assert "Recovered by OCR" in result.markdown


async def test_ocr_beat_is_called_off_the_event_loop(env: Path, monkeypatch) -> None:
    """Pins the precondition behind the watchdog's thread-safety: the parse runs
    in a worker thread, so the beat OCR fires per page has no running loop."""

    seen: dict[str, bool] = {}

    def _fake_ocr_pdf_pages(data, settings, beat, page_indices):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            seen["off_loop"] = True
        beat()
        return {0: "Recovered by OCR. " * 40}

    monkeypatch.setattr(
        file_extraction, "_parse_pdf", lambda _data, _first, _last: (["tiny"], {0}, {})
    )
    monkeypatch.setattr(ocr, "ocr_pdf_pages", _fake_ocr_pdf_pages)
    await _run(env, "scan.pdf", text_page_pdf("tiny"))
    assert seen.get("off_loop"), "OCR ran on the loop; the per-page beat would be loop-bound"


async def test_extract_text_pdf_does_not_invoke_ocr(env: Path, monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise AssertionError("OCR must not run for a text PDF")

    monkeypatch.setattr(ocr, "ocr_pdf_pages", _boom)
    result = await _run(env, "whitepaper.pdf", text_page_pdf(_LONG))
    assert "Lorem ipsum" in result.markdown


async def test_ocr_low_confidence_fails_with_clear_error(env: Path, monkeypatch) -> None:
    def _low(_data, _settings, _beat):
        raise ocr.OcrLowConfidenceError("OCR confidence 0.21 below floor 0.50")

    monkeypatch.setattr(ocr, "ocr_image", _low)
    with pytest.raises(ExtractionPermanentError, match="confidence"):
        await _run(env, "noise.png", b"not really a png")


def test_ocr_page_cap_counts_selected_pages_and_accepts_high_page_numbers(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OCR_MAX_PAGES", "2")
    get_settings.cache_clear()
    rendered: list[int] = []

    class _Page:
        def __init__(self, number: int) -> None:
            self.number = number

        def render(self, scale: float):
            rendered.append(self.number)
            return self

        def to_pil(self):
            return Image.new("RGB", (2, 2), "white")

    class _Document:
        def __init__(self, _data: bytes) -> None:
            pass

        def __len__(self) -> int:
            return 40

        def __getitem__(self, index: int):
            return _Page(index)

        def close(self) -> None:
            pass

    monkeypatch.setattr(ocr.pdfium, "PdfDocument", _Document)
    monkeypatch.setattr(ocr, "_run_page", lambda _image: ("recognized", [0.99]))
    pages = ocr.ocr_pdf_pages(b"pdf", get_settings(), lambda: None, [20, 39])
    assert set(pages) == {20, 39}
    assert rendered == [20, 39]


def test_full_pdf_ocr_fails_instead_of_truncating_over_cap(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OCR_MAX_PAGES", "2")
    get_settings.cache_clear()

    class _Document:
        def __init__(self, _data: bytes) -> None:
            pass

        def __len__(self) -> int:
            return 3

        def close(self) -> None:
            pass

    monkeypatch.setattr(ocr.pdfium, "PdfDocument", _Document)
    with pytest.raises(ocr.OcrError, match="refusing partial OCR"):
        ocr.ocr_pdf(b"pdf", get_settings(), lambda: None)


def test_ocr_fails_when_selected_scanned_page_has_no_readable_text(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Page:
        def render(self, scale: float):
            return self

        def to_pil(self):
            return Image.new("RGB", (2, 2), "white")

    class _Document:
        def __init__(self, _data: bytes) -> None:
            pass

        def __len__(self) -> int:
            return 40

        def __getitem__(self, _index: int):
            return _Page()

        def close(self) -> None:
            pass

    monkeypatch.setattr(ocr.pdfium, "PdfDocument", _Document)
    monkeypatch.setattr(ocr, "_run_page", lambda _image: ("", []))
    with pytest.raises(ocr.OcrLowConfidenceError, match="PDF page 40"):
        ocr.ocr_pdf_pages(b"pdf", get_settings(), lambda: None, [39])


def test_mixed_pdf_with_ocr_disabled_fails_instead_of_omitting_scans(env, monkeypatch):
    monkeypatch.setenv("OCR_ENABLED", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(
        file_extraction, "_parse_pdf", lambda _data, _first, _last: ([_LONG, ""], {1}, {})
    )
    with pytest.raises(ExtractionPermanentError, match="Enable OCR_ENABLED"):
        file_extraction._parse(b"pdf", ".pdf", get_settings(), lambda: None)


# --- chapter jobs (books) ------------------------------------------------------


def _epub_three_chapters() -> bytes:
    names = ["One", "Two", "Three"]
    return simple_epub(
        title="Book",
        author="Some Author",
        names=names,
        bodies=[f"Body of chapter {name.lower()}. {_LONG}" for name in names],
    )


def _store_book(
    env: Path, filename: str, data: bytes, *, has_cover: bool = False
) -> tuple[books.Book, list[books.Chapter]]:
    settings = get_settings()
    detected = book_chapters.detect(data, file_extraction.extension_of(filename), min_chars=10)
    assert detected is not None
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        uri = file_extraction.build_source_uri("c" * 16, filename)
        book = books.create_book(
            conn, source_uri=uri, filename=filename, detected=detected, has_cover=has_cover
        )
        chapters = books.get_chapters(conn, book.id)
    finally:
        conn.close()
    path = books.source_file(settings, book.id, file_extraction.extension_of(filename))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return book, chapters


def _chapter_job(book: books.Book, idx: int) -> jobs.Job:
    uri = books.chapter_uri(book.source_uri, idx)
    return jobs.Job(
        id="job1",
        url=uri,
        episode_id=jobs.compute_episode_id(uri),
        status="processing",
        stage="extract",
        error=None,
        created_at="t",
        updated_at="t",
    )


def test_parse_source_uri_ignores_chapter_fragment() -> None:
    uri = file_extraction.build_source_uri("abc123", "My Book.epub")
    assert file_extraction.parse_source_uri(books.chapter_uri(uri, 4)) == (
        "abc123",
        "My Book.epub",
    )


async def test_extract_epub_whole_book(env: Path) -> None:
    result = await _run(env, "novel.epub", _epub_three_chapters())
    assert result.metadata["title"] == "Book"
    assert result.metadata["author"] == "Some Author"
    for name in ("one", "two", "three"):
        assert f"Body of chapter {name}" in result.markdown


async def test_extract_epub_chapter_returns_only_that_chapter(env: Path) -> None:
    book, _ = _store_book(env, "novel.epub", _epub_three_chapters())
    result = await file_extraction.extract_file(_chapter_job(book, 2), get_settings())
    assert "Body of chapter two" in result.markdown
    assert "Body of chapter one" not in result.markdown
    assert "Body of chapter three" not in result.markdown
    assert result.metadata["title"] == "Book: Two"
    assert result.metadata["author"] == "Some Author"
    assert set(result.metadata) == {"title", "author"}


async def test_extract_pdf_chapter_reads_only_its_pages(env: Path) -> None:
    texts = [f"Marker{n} " + "filler words here. " * 12 for n in range(6)]
    data = pdf_book(texts, [("A", 0), ("B", 2), ("C", 4)])
    book, chapters = _store_book(env, "tome.pdf", data)
    assert chapters[1].locator == {"first_page": 2, "last_page": 3}
    result = await file_extraction.extract_file(_chapter_job(book, 2), get_settings())
    assert "Marker2" in result.markdown
    assert "Marker3" in result.markdown
    for n in (0, 1, 4, 5):
        assert f"Marker{n}" not in result.markdown
    assert result.metadata["title"] == "tome: B"


async def test_extract_pdf_chapter_ocr_limited_to_range(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    texts = ["tiny"] * 6
    data = pdf_book(texts, [("A", 0), ("B", 2), ("C", 4)])
    book, _ = _store_book(env, "scan.pdf", data)
    monkeypatch.setattr(file_extraction, "page_has_image", lambda _page: True)
    seen: dict[str, list[int]] = {}

    def _fake_ocr(_data, _settings, _beat, page_indices):
        seen["indices"] = list(page_indices)
        return {i: f"Recognized page {i}. " * 20 for i in page_indices}

    monkeypatch.setattr(ocr, "ocr_pdf_pages", _fake_ocr)
    result = await file_extraction.extract_file(_chapter_job(book, 2), get_settings())
    assert seen["indices"] == [2, 3]
    assert "Recognized page 2" in result.markdown
    assert "Recognized page 4" not in result.markdown


async def test_chapter_job_with_missing_book_source_is_permanent_error(env: Path) -> None:
    book, _ = _store_book(env, "novel.epub", _epub_three_chapters())
    books.source_file(get_settings(), book.id, ".epub").unlink()
    with pytest.raises(ExtractionPermanentError, match="stored upload missing") as exc:
        await file_extraction.extract_file(_chapter_job(book, 1), get_settings())
    assert book.id in str(exc.value)


async def test_chapter_job_with_missing_book_row_is_permanent_error(env: Path) -> None:
    database.run_migrations(env)
    uri = file_extraction.build_source_uri("e" * 16, "ghost.epub")
    job = jobs.Job(
        id="job1",
        url=books.chapter_uri(uri, 1),
        episode_id="x",
        status="processing",
        stage="extract",
        error=None,
        created_at="t",
        updated_at="t",
    )
    with pytest.raises(ExtractionPermanentError, match="stored upload missing"):
        await file_extraction.extract_file(job, get_settings())


def test_source_ref_stored_path_and_lock_key(env: Path) -> None:
    settings = get_settings()
    uri = file_extraction.build_source_uri("f" * 16, "novel.epub")
    book_id = books.book_id_for(uri)
    chapter = file_extraction.SourceRef.parse(books.chapter_uri(uri, 3), "ep")
    assert chapter.chapter == 3 and chapter.book_id == book_id
    assert chapter.stored_path(settings) == books.source_file(settings, book_id, ".epub")
    assert chapter.lock_key() == book_id
    plain = file_extraction.SourceRef.parse(uri, "ep")
    assert plain.chapter is None and plain.book_id is None
    assert plain.stored_path(settings) == file_extraction.source_path(settings, "ep", "novel.epub")
    assert plain.lock_key() == "ep"


def test_source_ref_for_a_web_url() -> None:
    ref = file_extraction.SourceRef.parse("https://example.test/a", "ep")
    assert (ref.is_upload, ref.filename, ref.chapter, ref.book_id) == (False, None, None, None)


def test_source_ref_ignores_chapter_fragment_on_web_urls() -> None:
    url = "https://example.test/post#chapter=3"
    ref = file_extraction.SourceRef.parse(url, "abc")
    assert ref.is_upload is False
    assert (ref.chapter, ref.book_id, ref.filename) == (None, None, None)
    assert ref.base_uri == url


async def test_unsplit_corrupt_epub_is_permanent_error(env: Path) -> None:
    with pytest.raises(ExtractionPermanentError, match="could not read EPUB"):
        await _run(env, "broken.epub", b"this is not a zip")


async def test_chapter_job_with_corrupt_book_source_is_permanent_error(env: Path) -> None:
    book, _ = _store_book(env, "novel.epub", _epub_three_chapters())
    books.source_file(get_settings(), book.id, ".epub").write_bytes(b"this is not a zip")
    with pytest.raises(ExtractionPermanentError, match="could not read EPUB"):
        await file_extraction.extract_file(_chapter_job(book, 2), get_settings())


def test_whole_file_pdf_ocr_indexes_are_unshifted(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = pdf_book(["Native text. " * 20, "tiny", "Native text. " * 20, "tiny"], [])
    monkeypatch.setattr(file_extraction, "page_has_image", lambda _page: True)
    seen: dict[str, list[int]] = {}

    def _fake_ocr(_data, _settings, _beat, page_indices):
        seen["indices"] = list(page_indices)
        return {i: f"Recognized page {i}. " * 20 for i in page_indices}

    monkeypatch.setattr(ocr, "ocr_pdf_pages", _fake_ocr)
    file_extraction._parse(data, ".pdf", get_settings(), lambda: None)
    assert seen["indices"] == [1, 3]
