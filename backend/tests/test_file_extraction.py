from __future__ import annotations

import io
from pathlib import Path

import docx
import pytest
from app.config import get_settings
from app.services import file_extraction, jobs
from app.services.extraction_types import ExtractionPermanentError, ExtractionTooShortError

_LONG = "Lorem ipsum dolor sit amet consectetur adipiscing elit. " * 20  # ~1100 chars


def _make_pdf(text: str) -> bytes:
    """Build a minimal single-page PDF whose content stream prints ``text`` so
    pypdf.extract_text returns it. Offsets are computed so the xref is valid."""

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
    result = await _run(env, "whitepaper.pdf", _make_pdf(_LONG))
    assert "Lorem ipsum" in result.markdown
    assert result.metadata["title"] == "whitepaper"


async def test_extract_docx_uses_core_property_title_and_author(env: Path) -> None:
    data = _make_docx(title="Doc Title", author="Jane Doe", body=_LONG + "\n\n" + _LONG)
    result = await _run(env, "report.docx", data)
    assert result.metadata["title"] == "Doc Title"
    assert result.metadata["author"] == "Jane Doe"
    assert "Lorem ipsum" in result.markdown


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
