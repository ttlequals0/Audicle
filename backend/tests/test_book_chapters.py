from __future__ import annotations

import io
import zipfile

import pytest
from app.services import book_chapters
from app.services.book_chapters import BookFormatError

NCX_TYPE = "application/x-dtbncx+xml"
XHTML = "application/xhtml+xml"


def make_epub(files: dict[str, bytes | str], *, opf_path="content.opf") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            f'<rootfile full-path="{opf_path}" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def opf(items: list[tuple[str, str, str, str]], spine: list[str], *, meta="", toc=None) -> str:
    """items: (id, href, media-type, properties)."""
    manifest = "".join(
        f'<item id="{i}" href="{h}" media-type="{t}"' + (f' properties="{p}"' if p else "") + "/>"
        for i, h, t, p in items
    )
    refs = "".join(f'<itemref idref="{s}"/>' for s in spine)
    toc_attr = f' toc="{toc}"' if toc else ""
    return (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"{meta}</metadata><manifest>{manifest}</manifest><spine{toc_attr}>{refs}</spine></package>"
    )


def page(body: str) -> str:
    return f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body>{body}</body></html>'


def ncx(entries: list[tuple[str, str]], nested: list[tuple[str, str]] | None = None) -> str:
    points = ""
    for n, (label, src) in enumerate(entries, 1):
        inner = ""
        if nested and n == 1:
            inner = "".join(
                f'<navPoint id="c{k}"><navLabel><text>{lb}</text></navLabel><content src="{s}"/></navPoint>'
                for k, (lb, s) in enumerate(nested)
            )
        points += (
            f'<navPoint id="p{n}" playOrder="{n}"><navLabel><text>{label}</text></navLabel>'
            f'<content src="{src}"/>{inner}</navPoint>'
        )
    return (
        '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        f"<navMap>{points}</navMap></ncx>"
    )


def simple_book(n=3, words=30, *, nested=None) -> bytes:
    files: dict[str, bytes | str] = {}
    items = [("ncx", "toc.ncx", NCX_TYPE, "")]
    for i in range(1, n + 1):
        files[f"ch{i}.xhtml"] = page(f"<h1>Chapter {i}</h1><p>{'word ' * words}</p>")
        items.append((f"c{i}", f"ch{i}.xhtml", XHTML, ""))
    files["toc.ncx"] = ncx([(f"Chapter {i}", f"ch{i}.xhtml") for i in range(1, n + 1)], nested)
    files["content.opf"] = opf(items, [f"c{i}" for i in range(1, n + 1)], toc="ncx")
    return make_epub(files)


def detect_epub(data: bytes, min_chars=10):
    return book_chapters.detect(data, ".epub", min_chars=min_chars)


def test_ncx_top_level_entries_become_chapters():
    book = detect_epub(simple_book(3))
    assert [c.idx for c in book.chapters] == [1, 2, 3]
    assert [c.title for c in book.chapters] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert book.format == "epub"


def test_nav_document_preferred_over_ncx():
    files = {
        "a.xhtml": page("<p>alpha alpha alpha</p>"),
        "b.xhtml": page("<p>bravo bravo bravo</p>"),
        "nav.xhtml": page(
            '<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc"><ol>'
            '<li><a href="a.xhtml">Nav One</a></li><li><a href="b.xhtml">Nav Two</a></li></ol></nav>'
        ),
        "toc.ncx": ncx([("Ncx One", "a.xhtml"), ("Ncx Two", "b.xhtml")]),
    }
    items = [
        ("ncx", "toc.ncx", NCX_TYPE, ""),
        ("nav", "nav.xhtml", XHTML, "nav"),
        ("a", "a.xhtml", XHTML, ""),
        ("b", "b.xhtml", XHTML, ""),
    ]
    files["content.opf"] = opf(items, ["a", "b"], toc="ncx")
    book = detect_epub(make_epub(files))
    assert [c.title for c in book.chapters] == ["Nav One", "Nav Two"]


def test_nested_entries_fold_into_parent():
    files: dict[str, bytes | str] = {
        "ch1.xhtml": page("<h1>One</h1><p>parent text</p>"),
        "ch1b.xhtml": page("<p>child section text</p>"),
        "ch2.xhtml": page("<h1>Two</h1><p>second text</p>"),
    }
    files["toc.ncx"] = ncx(
        [("One", "ch1.xhtml"), ("Two", "ch2.xhtml")],
        nested=[("One B", "ch1b.xhtml")],
    )
    items = [("ncx", "toc.ncx", NCX_TYPE, "")] + [
        (k, f"{k}.xhtml", XHTML, "") for k in ("ch1", "ch1b", "ch2")
    ]
    files["content.opf"] = opf(items, ["ch1", "ch1b", "ch2"], toc="ncx")
    data = make_epub(files)
    book = detect_epub(data)
    assert [c.title for c in book.chapters] == ["One", "Two"]
    md = book_chapters.epub_chapter_markdown(data, book.chapters[0].locator)
    assert "parent text" in md and "child section text" in md
    assert "second text" not in md


def test_entry_spanning_several_spine_files():
    files: dict[str, bytes | str] = {"a.xhtml": page("<p>intro intro</p>")}
    items = [("ncx", "toc.ncx", NCX_TYPE, ""), ("a", "a.xhtml", XHTML, "")]
    spine = ["a"]
    for i in range(1, 4):
        files[f"n{i}.xhtml"] = page(f"<p>note{i} one two three</p>")
        items.append((f"n{i}", f"n{i}.xhtml", XHTML, ""))
        spine.append(f"n{i}")
    files["z.xhtml"] = page("<p>last last</p>")
    items.append(("z", "z.xhtml", XHTML, ""))
    spine.append("z")
    files["toc.ncx"] = ncx([("Intro", "a.xhtml"), ("Notes", "n1.xhtml"), ("Last", "z.xhtml")])
    files["content.opf"] = opf(items, spine, toc="ncx")
    data = make_epub(files)
    book = detect_epub(data)
    notes = book.chapters[1]
    md = book_chapters.epub_chapter_markdown(data, notes.locator)
    assert all(f"note{i}" in md for i in (1, 2, 3))
    assert "last last" not in md
    assert notes.word_count == 12


def _anchor_book(second_anchor: str) -> bytes:
    files: dict[str, bytes | str] = {
        "a.xhtml": page(
            '<h2 id="s1">First</h2><p>first body text</p><h2 id="s2">Second</h2><p>second body text</p>'
        ),
        "b.xhtml": page("<p>other chapter text</p>"),
    }
    files["toc.ncx"] = ncx(
        [("First", "a.xhtml#s1"), ("Second", f"a.xhtml#{second_anchor}"), ("Other", "b.xhtml")]
    )
    items = [
        ("ncx", "toc.ncx", NCX_TYPE, ""),
        ("a", "a.xhtml", XHTML, ""),
        ("b", "b.xhtml", XHTML, ""),
    ]
    files["content.opf"] = opf(items, ["a", "b"], toc="ncx")
    return make_epub(files)


def test_two_entries_in_one_file_split_at_anchor():
    data = _anchor_book("s2")
    book = detect_epub(data)
    assert [c.title for c in book.chapters] == ["First", "Second", "Other"]
    first = book_chapters.epub_chapter_markdown(data, book.chapters[0].locator)
    second = book_chapters.epub_chapter_markdown(data, book.chapters[1].locator)
    assert "first body text" in first and "second body text" not in first
    assert "second body text" in second and "first body text" not in second


def test_missing_anchor_merges_entries():
    data = _anchor_book("nope")
    book = detect_epub(data)
    assert [c.title for c in book.chapters] == ["First", "Other"]
    md = book_chapters.epub_chapter_markdown(data, book.chapters[0].locator)
    assert "first body text" in md and "second body text" in md


def test_no_toc_falls_back_to_spine_documents():
    files: dict[str, bytes | str] = {
        "a.xhtml": page("<h1>Opening</h1><p>some text here</p>"),
        "b.xhtml": page("<p>no heading here</p>"),
    }
    items = [("a", "a.xhtml", XHTML, ""), ("b", "b.xhtml", XHTML, "")]
    files["content.opf"] = opf(items, ["a", "b"])
    book = detect_epub(make_epub(files))
    assert [c.title for c in book.chapters] == ["Opening", "Section 2"]


def test_default_selection():
    titles = [
        ("Copyright", "legal legal legal legal"),
        ("Notes", "notes notes notes notes"),
        ("Table of Contents", "toc toc toc toc"),
        ("About the Author", "bio bio bio bio"),
        ("Tiny", "abc"),
        ("Prologue", "prologue text here"),
        ("1. A Purposeful Life", "life text here"),
        ("Epilogue", "epilogue text here"),
        ("Author's Note", "author note text"),
    ]
    files: dict[str, bytes | str] = {}
    items = [("ncx", "toc.ncx", NCX_TYPE, "")]
    for i, (_, body) in enumerate(titles):
        files[f"f{i}.xhtml"] = page(f"<p>{body}</p>")
        items.append((f"f{i}", f"f{i}.xhtml", XHTML, ""))
    files["toc.ncx"] = ncx([(t, f"f{i}.xhtml") for i, (t, _) in enumerate(titles)])
    files["content.opf"] = opf(items, [f"f{i}" for i in range(len(titles))], toc="ncx")
    book = detect_epub(make_epub(files), min_chars=10)
    selected = {c.title: c.default_selected for c in book.chapters}
    for t in ("Copyright", "Notes", "Table of Contents", "About the Author", "Tiny"):
        assert selected[t] is False, t
    for t in ("Prologue", "1. A Purposeful Life", "Epilogue", "Author's Note"):
        assert selected[t] is True, t


def _with_encryption(xml: str) -> bytes:
    files: dict[str, bytes | str] = {}
    items = [("ncx", "toc.ncx", NCX_TYPE, "")]
    for i in (1, 2):
        files[f"ch{i}.xhtml"] = page(f"<p>chapter {i} text text</p>")
        items.append((f"c{i}", f"ch{i}.xhtml", XHTML, ""))
    files["toc.ncx"] = ncx([("One", "ch1.xhtml"), ("Two", "ch2.xhtml")])
    files["content.opf"] = opf(items, ["c1", "c2"], toc="ncx")
    files["META-INF/encryption.xml"] = xml
    return make_epub(files)


def _encryption_xml(uri: str) -> str:
    return (
        '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
        'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
        f'<enc:CipherData><enc:CipherReference URI="{uri}"/></enc:CipherData>'
        "</enc:EncryptedData></encryption>"
    )


def test_font_only_encryption_xml_is_accepted():
    book = detect_epub(_with_encryption(_encryption_xml("fonts/a.otf")))
    assert len(book.chapters) == 2


def test_encrypted_spine_document_is_rejected():
    with pytest.raises(BookFormatError, match="DRM"):
        detect_epub(_with_encryption(_encryption_xml("ch1.xhtml")))


def test_oversize_archive_rejected(monkeypatch):
    monkeypatch.setattr(book_chapters, "MAX_UNCOMPRESSED_BYTES", 100)
    with pytest.raises(BookFormatError, match="too large"):
        detect_epub(simple_book(2))


def test_too_many_entries_rejected(monkeypatch):
    monkeypatch.setattr(book_chapters, "MAX_ARCHIVE_ENTRIES", 3)
    with pytest.raises(BookFormatError, match="too many"):
        detect_epub(simple_book(2))


def test_traversal_entry_rejected():
    data = make_epub({"../evil.html": "x"})
    with pytest.raises(BookFormatError, match="unsafe"):
        detect_epub(data)


def test_not_a_zip_rejected():
    with pytest.raises(BookFormatError):
        detect_epub(b"this is not a zip")


def test_missing_container_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
    with pytest.raises(BookFormatError, match="container"):
        detect_epub(buf.getvalue())


def test_opf_in_subfolder_and_encoded_hrefs():
    files: dict[str, bytes | str] = {
        "OEBPS/Text/ch 1.xhtml": page("<h1>One</h1><p>first chapter words</p>"),
        "OEBPS/Text/ch 2.xhtml": page("<h1>Two</h1><p>second chapter words</p>"),
        "OEBPS/nav/nav.xhtml": page(
            '<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc"><ol>'
            '<li><a href="../Text/ch%201.xhtml">Uno</a></li>'
            '<li><a href="../Text/ch%202.xhtml">Dos</a></li></ol></nav>'
        ),
    }
    items = [
        ("nav", "nav/nav.xhtml", XHTML, "nav"),
        ("c1", "Text/ch%201.xhtml", XHTML, ""),
        ("c2", "Text/ch%202.xhtml", XHTML, ""),
    ]
    files["OEBPS/content.opf"] = opf(items, ["c1", "c2"])
    data = make_epub(files, opf_path="OEBPS/content.opf")
    book = detect_epub(data)
    assert [c.title for c in book.chapters] == ["Uno", "Dos"]
    assert "first chapter words" in book_chapters.epub_chapter_markdown(
        data, book.chapters[0].locator
    )


def test_toc_entry_outside_spine_or_out_of_order_is_skipped():
    files: dict[str, bytes | str] = {
        "a.xhtml": page("<p>aaa aaa aaa</p>"),
        "b.xhtml": page("<p>bbb bbb bbb</p>"),
        "c.xhtml": page("<p>ccc ccc ccc</p>"),
        "orphan.xhtml": page("<p>not in spine</p>"),
    }
    files["toc.ncx"] = ncx(
        [
            ("A", "a.xhtml"),
            ("Orphan", "orphan.xhtml"),
            ("C", "c.xhtml"),
            ("Back", "b.xhtml"),
            ("Ghost", "gone.xhtml"),
        ]
    )
    items = [("ncx", "toc.ncx", NCX_TYPE, "")] + [
        (k, f"{k}.xhtml", XHTML, "") for k in ("a", "b", "c", "orphan")
    ]
    files["content.opf"] = opf(items, ["a", "b", "c"], toc="ncx")
    book = detect_epub(make_epub(files))
    assert [c.title for c in book.chapters] == ["A", "C"]
    for c in book.chapters:
        assert c.locator["start_spine"] <= c.locator["end_spine"]
        assert c.char_count > 0


def test_non_utf8_and_bodiless_xhtml():
    files: dict[str, bytes | str] = {
        "a.xhtml": (
            b'<?xml version="1.0" encoding="ISO-8859-1"?>'
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>caf\xe9 au lait</p></body></html>'
        ),
        "b.xhtml": b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>x</title></head></html>',
        "c.xhtml": page("<p>closing words</p>"),
    }
    items = [(k, f"{k}.xhtml", XHTML, "") for k in ("a", "b", "c")]
    files["content.opf"] = opf(items, ["a", "b", "c"])
    text, _ = book_chapters.epub_full_markdown(make_epub(files))
    assert "caf\u00e9 au lait" in text
    assert text == "caf\u00e9 au lait\n\nclosing words"
    body_only = make_epub(
        {"b.xhtml": files["b.xhtml"], "content.opf": opf([("b", "b.xhtml", XHTML, "")], ["b"])}
    )
    assert book_chapters.epub_full_markdown(body_only)[0] == ""


def test_cover_bytes_returned():
    files: dict[str, bytes | str] = {"cover.jpg": b"\xff\xd8\xffJPEGDATA"}
    for i in (1, 2):
        files[f"ch{i}.xhtml"] = page(f"<p>chapter {i} text</p>")
    files["toc.ncx"] = ncx([("One", "ch1.xhtml"), ("Two", "ch2.xhtml")])
    items = [
        ("ncx", "toc.ncx", NCX_TYPE, ""),
        ("c1", "ch1.xhtml", XHTML, ""),
        ("c2", "ch2.xhtml", XHTML, ""),
        ("img", "cover.jpg", "image/jpeg", ""),
    ]
    files["content.opf"] = opf(
        items, ["c1", "c2"], meta='<meta name="cover" content="img"/>', toc="ncx"
    )
    assert detect_epub(make_epub(files)).cover == b"\xff\xd8\xffJPEGDATA"
    items[3] = ("img", "cover.jpg", "image/jpeg", "cover-image")
    files["content.opf"] = opf(items, ["c1", "c2"], toc="ncx")
    assert detect_epub(make_epub(files)).cover == b"\xff\xd8\xffJPEGDATA"


def test_metadata_title_and_author():
    files: dict[str, bytes | str] = {}
    for i in (1, 2):
        files[f"ch{i}.xhtml"] = page(f"<p>chapter {i} text</p>")
    files["toc.ncx"] = ncx([("One", "ch1.xhtml"), ("Two", "ch2.xhtml")])
    items = [
        ("ncx", "toc.ncx", NCX_TYPE, ""),
        ("c1", "ch1.xhtml", XHTML, ""),
        ("c2", "ch2.xhtml", XHTML, ""),
    ]
    meta = "<dc:title>Bad Blood</dc:title><dc:creator>John Carreyrou</dc:creator>"
    files["content.opf"] = opf(items, ["c1", "c2"], meta=meta, toc="ncx")
    book = detect_epub(make_epub(files))
    assert (book.title, book.author, book.cover) == ("Bad Blood", "John Carreyrou", None)


def test_epub_full_markdown_is_whole_spine_in_order():
    data = simple_book(3, words=2)
    text, meta = book_chapters.epub_full_markdown(data)
    assert text.index("# Chapter 1") < text.index("# Chapter 2") < text.index("# Chapter 3")
    assert meta == {"title": None, "author": None}


def test_single_chapter_epub_detects_none():
    assert detect_epub(simple_book(1)) is None


def test_unsupported_extension_detects_none():
    assert book_chapters.detect(b"x", ".docx", min_chars=10) is None


def test_markdown_walk_drops_noise_and_keeps_blocks():
    body = (
        '<h2>Head</h2><p>para <em>one</em><span epub:type="pagebreak">99</span></p>'
        "<script>bad()</script><aside>side</aside><blockquote>quoted</blockquote>"
        "<ul><li>item a</li><li>item b</li></ul><div>loose text</div>"
    )
    doc = (
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        f"<body>{body}</body></html>"
    )
    files: dict[str, bytes | str] = {
        "a.xhtml": doc,
        "content.opf": opf([("a", "a.xhtml", XHTML, "")], ["a"]),
    }
    text, _ = book_chapters.epub_full_markdown(make_epub(files))
    assert text == "## Head\n\npara one\n\nquoted\n\nitem a\n\nitem b\n\nloose text"


def _two_chapter_files() -> dict[str, bytes | str]:
    files: dict[str, bytes | str] = {}
    for i in (1, 2):
        files[f"ch{i}.xhtml"] = page(f"<p>chapter {i} text text</p>")
    files["toc.ncx"] = ncx([("One", "ch1.xhtml"), ("Two", "ch2.xhtml")])
    return files


_TWO_ITEMS = [
    ("ncx", "toc.ncx", NCX_TYPE, ""),
    ("c1", "ch1.xhtml", XHTML, ""),
    ("c2", "ch2.xhtml", XHTML, ""),
]


def test_duplicate_spine_itemrefs_are_deduplicated():
    files = {"a.xhtml": page("<p>only once</p>")}
    files["content.opf"] = opf([("a", "a.xhtml", XHTML, "")], ["a", "a", "a"])
    text, _ = book_chapters.epub_full_markdown(make_epub(files))
    assert text == "only once"


def test_oversize_spine_rejected(monkeypatch):
    files = {"a.xhtml": page("<p>x</p>")}
    files["content.opf"] = opf([("a", "a.xhtml", XHTML, "")], ["a"] * 6)
    data = make_epub(files)
    monkeypatch.setattr(book_chapters, "MAX_ARCHIVE_ENTRIES", 6)
    assert book_chapters.epub_full_markdown(data)[0] == "x"
    monkeypatch.setattr(book_chapters, "MAX_ARCHIVE_ENTRIES", 5)
    with pytest.raises(BookFormatError, match="spine"):
        book_chapters.epub_full_markdown(data)


def test_oversize_toc_rejected(monkeypatch):
    nested = [(f"n{i}", "ch1.xhtml") for i in range(5)]
    data = simple_book(3, nested=nested)  # 7 archive entries, 8 navPoints
    monkeypatch.setattr(book_chapters, "MAX_ARCHIVE_ENTRIES", 8)
    assert len(detect_epub(data).chapters) == 3
    monkeypatch.setattr(book_chapters, "MAX_ARCHIVE_ENTRIES", 7)
    with pytest.raises(BookFormatError, match="table of contents"):
        detect_epub(data)


def test_extracted_text_cap_rejected(monkeypatch):
    data = simple_book(3, words=30)
    monkeypatch.setattr(book_chapters, "MAX_EXTRACTED_CHARS", 100)
    with pytest.raises(BookFormatError, match="too much text"):
        detect_epub(data)
    with pytest.raises(BookFormatError, match="too much text"):
        book_chapters.epub_full_markdown(data)
    locator = {"start_spine": 0, "start_anchor": None, "end_spine": 3, "end_anchor": None}
    with pytest.raises(BookFormatError, match="too much text"):
        book_chapters.epub_chapter_markdown(data, locator)


def test_truncated_member_raises_book_format_error(monkeypatch):
    data = simple_book(2)

    def boom(self, *a, **k):
        raise EOFError("truncated")

    monkeypatch.setattr(zipfile.ZipFile, "open", boom)
    with pytest.raises(BookFormatError):
        detect_epub(data)


def test_image_spine_item_is_not_read(monkeypatch):
    files: dict[str, bytes | str] = {"cover.jpg": b"\xff\xd8", **_two_chapter_files()}
    items = [("img", "cover.jpg", "image/jpeg", ""), *_TWO_ITEMS]
    files["content.opf"] = opf(items, ["img", "c1", "c2"], toc="ncx")
    data = make_epub(files)
    reads: list[str] = []
    original = book_chapters._Epub._read

    def spy(self, name):
        reads.append(name)
        return original(self, name)

    monkeypatch.setattr(book_chapters._Epub, "_read", spy)
    assert len(detect_epub(data).chapters) == 2
    assert "cover.jpg" not in reads


def test_malformed_ncx_falls_back_to_spine_documents():
    files = _two_chapter_files()
    files["toc.ncx"] = "<ncx><navMap>"
    files["content.opf"] = opf(_TWO_ITEMS, ["c1", "c2"], toc="ncx")
    book = detect_epub(make_epub(files))
    assert [c.title for c in book.chapters] == ["Section 1", "Section 2"]


def test_cover_falls_back_to_cover_image_property():
    files = {"cover.jpg": b"\xff\xd8COVER", **_two_chapter_files()}
    items = [*_TWO_ITEMS, ("img", "cover.jpg", "image/jpeg", "cover-image")]
    meta = '<meta name="cover" content="c1"/>'
    files["content.opf"] = opf(items, ["c1", "c2"], meta=meta, toc="ncx")
    assert detect_epub(make_epub(files)).cover == b"\xff\xd8COVER"


def test_non_epub_type_attribute_is_not_dropped():
    doc = (
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:x="urn:other"><body>'
        '<p x:type="pagebreak">kept text</p></body></html>'
    )
    files: dict[str, bytes | str] = {
        "a.xhtml": doc,
        "content.opf": opf([("a", "a.xhtml", XHTML, "")], ["a"]),
    }
    assert book_chapters.epub_full_markdown(make_epub(files))[0] == "kept text"
