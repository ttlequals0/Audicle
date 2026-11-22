"""Shared pytest fixtures.

Sets a minimal valid env for the Pydantic Settings model so individual tests
don't have to repeat the boilerplate. Tests that need to vary a specific value
override it with monkeypatch and call ``get_settings.cache_clear()``.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from app.config import get_settings
from pypdf import PdfReader, PdfWriter

_REQUIRED = {
    "BASE_URL": "https://audifeed.example.test",
    "UI_BASE_URL": "https://audicle.example.test",
    "FIRECRAWL_URL": "http://firecrawl.test:3002",
    "TTS_URL": "http://tts.test:8000",
    "LLM_PROVIDER": "openai-compatible",
    "LLM_MODEL": "qwen-test",
    "OPENAI_BASE_URL": "http://llm.test/v1",
    "OPENAI_API_KEY": "test-key",
    "FEED_TITLE": "Test Feed",
    "FEED_DESCRIPTION": "Test description",
    "FEED_AUTHOR": "Test Author",
    "FEED_EMAIL": "test@example.test",
    "FEED_ARTWORK_URL": "https://audifeed.example.test/static/art.png",
}

_EXTERNAL_CREDENTIALS = (
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "TTS_API_KEY",
    "WHISPER_API_KEY",
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_ssrf: run with the real SSRF resolver instead of the public-IP stub",
    )


@pytest.fixture(autouse=True)
def _stub_ssrf_resolver(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests submit placeholder URLs (``example.test``) that don't resolve;
    the SSRF guard on /submit and in extraction would reject them on DNS failure.
    Stub the resolver to a public IP so the guard passes through. Tests that
    exercise the guard itself opt out with the ``real_ssrf`` marker."""

    if request.node.get_closest_marker("real_ssrf"):
        return

    from app.services import ssrf

    async def _resolve_public(_host: str) -> str:
        return "203.0.113.1"

    monkeypatch.setattr(ssrf, "resolve_public_host", _resolve_public)


@pytest.fixture(autouse=True)
def _reset_login_rate_limiter():
    """The slowapi limiter on the login route is a module-level singleton;
    its in-memory store accumulates hits across tests and eventually 429s.
    Reset between each test so per-test counters start at zero."""

    try:
        from app.api.v1.auth import _LOGIN_LIMITER

        _LOGIN_LIMITER.reset()
    except ImportError:
        pass
    yield


@pytest.fixture(autouse=True)
def _default_voice_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In production a voice is always loaded (migration 018 fills slot 1), so the
    submit/upload guard expects at least one filled slot. Point the voices dir at an
    isolated temp folder with slot 1 filled, and never the repo. Tests that exercise
    the empty-slots / slot-management paths re-monkeypatch ``voices.voices_dir`` to
    their own dir; that runs after this autouse fixture, so their override wins."""

    from app.services import voices

    d = tmp_path / "_voice_slots"
    d.mkdir()
    (d / "slot1.wav").write_bytes(b"RIFFFAKEWAVE")
    monkeypatch.setattr(voices, "voices_dir", lambda: d)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Stand up a minimal valid env in tmp_path and reset the settings cache.

    Also chdir into tmp_path so pydantic-settings doesn't pick up a real .env
    sitting in the repo root. Clears the lru_cache on teardown so the next
    test's Settings is rebuilt against its own monkeypatched env.
    """

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    for key in _EXTERNAL_CREDENTIALS:
        monkeypatch.delenv(key, raising=False)
    for key, value in _REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("LOG_FORMAT", "text")
    # Don't import the ~400k-row bundled base lexicon on every app/worker startup
    # in tests; the migration's seed layer suffices and test_lexicon covers sync.
    monkeypatch.setenv("AUDICLE_SKIP_LEXICON_SYNC", "1")
    # TestClient speaks plain http; the production default (secure cookies)
    # would drop the session cookie so login wouldn't persist in tests.
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    # Reachability checks in tests must finish fast; tests that need a longer
    # grace can override these in their own monkeypatch.
    monkeypatch.setenv("TTS_REACHABILITY_GRACE_SECONDS", "0.5")
    monkeypatch.setenv("TTS_REACHABILITY_PROBE_TIMEOUT", "0.5")
    get_settings.cache_clear()
    try:
        yield data_dir
    finally:
        get_settings.cache_clear()


_ARCHIVE_TODAY_CAPTURE = (
    "example.com.\n\nexample.com\narchived 24 Sep 2026 01:26:47 UTC\narchive.today\n"
    "webpage capture\nSaved from\nno other snapshots from this url\n24 Sep 2026 01:26:47 UTC\n"
    "All snapshots\nfrom host\nwww.example.com\nWebpage\nScreenshot\nshare\ndownload .zip\n"
    "report bug or abuse\nBuy me a coffee\nReddit\nTwitter\nshort link\nlong link\nmarkdown\n"
    "html code\n example.com archived 24 Sep 2026 01:26:47 UTC\nwiki code\n"
    "{{cite web | title = example.com | url = https://www.example.com/economy/"
    "a-long-article-slug-c60716dd | date = 2026-09-24 | archiveurl = "
    "http://archive.today/Pe55U | archivedate = 2026-09-24 }}\n"
    "Please enable JS and disable any ad blocker\n"
    + "".join(f"{p}%\n" for p in range(0, 101, 10))
)


@pytest.fixture
def archive_capture_junk() -> str:
    """Extraction output that clears the length floor with no article in it: an
    archive.today snapshot whose capture was a bot wall (seen in prod on 0.58.0)."""

    return _ARCHIVE_TODAY_CAPTURE


def make_epub(files: dict[str, bytes | str], *, opf_path: str = "content.opf") -> bytes:
    """Zip ``files`` into an EPUB container whose package document is at ``opf_path``."""

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


def simple_epub(
    n: int = 3,
    *,
    title: str | None = "Test Book",
    author: str | None = "Test Author",
    cover: bytes | None = None,
    encrypted: bool = False,
    words: tuple[int, ...] = (),
    names: list[str] | None = None,
    bodies: list[str] | None = None,
) -> bytes:
    """An EPUB 2 book with an NCX and one chapter per spine item.

    ``names`` and ``bodies`` override the chapter headings and paragraph text;
    ``words`` gives per-chapter filler word counts (60 by default).
    """

    n = len(names) if names else n
    ncx_type, xhtml = "application/x-dtbncx+xml", "application/xhtml+xml"
    items = [f'<item id="ncx" href="toc.ncx" media-type="{ncx_type}"/>']
    points = ""
    files: dict[str, bytes | str] = {}
    for i in range(1, n + 1):
        name = names[i - 1] if names else f"Chapter {i}"
        text = bodies[i - 1] if bodies else "word " * (words[i - 1] if words else 60)
        files[f"ch{i}.xhtml"] = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>'
            f"<body><h1>{name}</h1><p>{text}</p></body></html>"
        )
        items.append(f'<item id="c{i}" href="ch{i}.xhtml" media-type="{xhtml}"/>')
        points += (
            f'<navPoint id="p{i}" playOrder="{i}"><navLabel><text>{name}</text></navLabel>'
            f'<content src="ch{i}.xhtml"/></navPoint>'
        )
    meta = (f"<dc:title>{title}</dc:title>" if title else "") + (
        f"<dc:creator>{author}</dc:creator>" if author else ""
    )
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
    return make_epub(files)


def text_page_pdf(text: str) -> bytes:
    """A minimal single-page PDF whose content stream prints ``text`` (valid xref)."""

    stream = b"BT /F1 24 Tf 72 720 Td (" + text.encode("latin-1") + b") Tj ET"
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


def pdf_book(texts: list[str], outline: list[tuple[str, int]]) -> bytes:
    """A PDF with one page per entry of ``texts`` and a top-level outline item per entry."""

    writer = PdfWriter()
    for text in texts:
        writer.add_page(PdfReader(io.BytesIO(text_page_pdf(text))).pages[0])
    for title, page in outline:
        writer.add_outline_item(title, page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
