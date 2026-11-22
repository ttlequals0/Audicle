"""Chapter detection and text extraction for uploaded books (EPUB and PDF).

Pure functions over file bytes: no database, settings, or network. An EPUB is
read with zipfile, defusedxml (container, OPF, NCX) and lxml (XHTML, nav). A PDF is
read with pypdf (bookmark outline).
"""

from __future__ import annotations

import io
import lzma
import posixpath
import re
import zipfile
import zlib
from dataclasses import dataclass
from functools import cached_property
from html.entities import html5
from typing import Any
from urllib.parse import unquote

import defusedxml.ElementTree as DET
from defusedxml import DefusedXmlException
from lxml import etree
from lxml import html as lxml_html
from pypdf import PdfReader
from pypdf.errors import PyPdfError

MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 5000
MAX_EXTRACTED_CHARS = 50 * 1024 * 1024

_MATTER_TITLES = frozenset(
    {
        "cover",
        "title page",
        "half title",
        "copyright",
        "contents",
        "table of contents",
        "dedication",
        "epigraph",
        "notes",
        "endnotes",
        "index",
        "bibliography",
        "references",
        "acknowledgments",
        "acknowledgements",
        "about the author",
        "also by",
        "praise",
    }
)

_HEADINGS = {f"h{n}": n for n in range(1, 7)}
_BLOCKS = frozenset(
    {
        "p",
        "div",
        "blockquote",
        "li",
        "ul",
        "ol",
        "dl",
        "dt",
        "dd",
        "table",
        "thead",
        "tbody",
        "tr",
        "td",
        "th",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "pre",
        "figure",
        "figcaption",
        "hr",
        "body",
        "form",
        "address",
        "details",
        "summary",
    }
)
_DROPPED = frozenset({"script", "style", "nav", "aside"})
_DROPPED_EPUB_TYPES = frozenset({"pagebreak", "noteref"})
_OPS_TYPE_ATTR = "{http://www.idpf.org/2007/ops}type"
_NCX_MEDIA_TYPE = "application/x-dtbncx+xml"
_XML_PARSE_ERRORS = (DET.ParseError, DefusedXmlException)


class BookFormatError(ValueError):
    """Message is shown to the operator in a 400."""


@dataclass(frozen=True)
class DetectedChapter:
    idx: int
    title: str
    locator: dict[str, Any]
    word_count: int
    char_count: int
    default_selected: bool
    selectable: bool


@dataclass(frozen=True)
class DetectedBook:
    format: str
    title: str | None
    author: str | None
    cover: bytes | None
    chapters: list[DetectedChapter]


def page_has_image(page: Any) -> bool:
    """Whether a PDF page contains an image XObject, including nested forms."""

    def visit(resources: Any, seen: set[int]) -> bool:
        if resources is None:
            return False
        if hasattr(resources, "get_object"):
            resources = resources.get_object()
        xobjects = resources.get("/XObject")
        if xobjects is None:
            return False
        for reference in xobjects.values():
            item = reference.get_object() if hasattr(reference, "get_object") else reference
            if id(item) in seen:
                continue
            seen.add(id(item))
            subtype = item.get("/Subtype")
            if subtype == "/Image":
                return True
            if subtype == "/Form" and visit(item.get("/Resources"), seen):
                return True
        return False

    return visit(page.get("/Resources"), set())


def is_front_or_back_matter(title: str) -> bool:
    kept = "".join(c for c in title.lower() if c.isalnum() or c == " ")
    return " ".join(kept.split()) in _MATTER_TITLES


def _local(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _clean(text: str) -> str:
    return " ".join(text.split())


def _epub_types(el: Any) -> set[str]:
    for key, value in el.attrib.items():
        if key in ("epub:type", _OPS_TYPE_ATTR):
            return set(value.split())
    return set()


def _is_dropped(el: Any, tag: str) -> bool:
    return tag == "" or tag in _DROPPED or bool(_epub_types(el) & _DROPPED_EPUB_TYPES)


def _entity_text(el: Any) -> str:
    # Only standard HTML names resolve; custom DTD entities stay dropped.
    return html5.get(str(el.text or "").strip("&;") + ";", "")


def _inline(el: Any) -> str:
    parts = [el.text or ""]
    for child in el:
        tag = _local(child.tag)
        if child.tag is etree.Entity:
            parts.append(_entity_text(child))
        elif not _is_dropped(child, tag):
            parts.append(" " if tag == "br" else _inline(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _note_anchors(el: Any, out: list[str], marks: dict[str, int]) -> None:
    for key in (el.get("id"), el.get("name")):
        if key:
            marks.setdefault(key, len(out))


def _walk(el: Any, out: list[str], marks: dict[str, int]) -> None:
    buf: list[str] = [el.text or ""]

    def flush() -> None:
        text = _clean("".join(buf))
        buf.clear()
        if text:
            out.append(text)

    for child in el:
        tag = _local(child.tag)
        if child.tag is etree.Entity:
            buf.append(_entity_text(child))
        elif _is_dropped(child, tag):
            pass
        elif tag in _HEADINGS:
            flush()
            for node in child.iter():
                _note_anchors(node, out, marks)
            text = _clean(_inline(child))
            if text:
                out.append("#" * _HEADINGS[tag] + " " + text)
        elif tag in _BLOCKS:
            flush()
            _note_anchors(child, out, marks)
            _walk(child, out, marks)
        else:
            for node in child.iter():
                _note_anchors(node, out, marks)
            buf.append(_inline(child))
        buf.append(child.tail or "")
    flush()


def _decode(data: bytes) -> str:
    head = data[:1024].decode("ascii", errors="ignore")
    match = re.search(r"""(?:encoding|charset)\s*=\s*["']?([\w-]+)""", head, re.I)
    for enc in (match.group(1) if match else "utf-8", "utf-8", "cp1252"):
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _parse_xhtml(data: bytes) -> Any | None:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        root = etree.fromstring(data, parser)
        if root is not None:
            return root
    except etree.XMLSyntaxError:
        pass
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", _decode(data))
    try:
        return lxml_html.document_fromstring(text)
    except (etree.ParserError, ValueError):
        return None


def _xml_children(el: Any, name: str) -> list[Any]:
    return [c for c in el if _local(c.tag) == name]


def _xml_find(el: Any, name: str) -> Any | None:
    for node in el.iter():
        if _local(node.tag) == name:
            return node
    return None


def _split_href(base: str, href: str) -> tuple[str, str | None]:
    path, _, frag = unquote(href).partition("#")
    if path:
        path = posixpath.normpath(posixpath.join(posixpath.dirname(base), path))
    return path, frag or None


class _Epub:
    def __init__(self, data: bytes) -> None:
        try:
            self._zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise BookFormatError("The file is not a valid EPUB archive.") from exc
        self._check_archive()
        self._docs: dict[str, tuple[list[str], dict[str, int]]] = {}
        self._chars = 0
        self._read_package()
        self._check_encryption()

    def _check_archive(self) -> None:
        infos = self._zf.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise BookFormatError("The EPUB archive has too many entries.")
        if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_BYTES:
            raise BookFormatError("The EPUB archive is too large once uncompressed.")
        for info in infos:
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise BookFormatError("The EPUB archive contains an unsafe entry name.")
        self._sizes = {i.filename: i.file_size for i in infos}

    def _read(self, name: str) -> bytes | None:
        size = self._sizes.get(name)
        if size is None:
            return None
        try:
            with self._zf.open(name) as fh:
                buf = fh.read(size + 1)
        except (
            zipfile.BadZipFile,
            NotImplementedError,
            RuntimeError,
            EOFError,
            OSError,
            lzma.LZMAError,
            zlib.error,
        ) as exc:
            raise BookFormatError("The EPUB archive could not be read.") from exc
        if len(buf) > size:
            raise BookFormatError("The EPUB archive has an inconsistent entry size.")
        return buf

    def _read_xml(self, name: str) -> Any | None:
        raw = self._read(name)
        if raw is None:
            return None
        try:
            return DET.fromstring(raw)
        except _XML_PARSE_ERRORS as exc:
            raise BookFormatError(f"The EPUB file {name} is not valid XML.") from exc

    def _read_package(self) -> None:
        container = self._read_xml("META-INF/container.xml")
        if container is None:
            raise BookFormatError("The EPUB archive has no META-INF/container.xml.")
        rootfile = _xml_find(container, "rootfile")
        self._opf_path = (
            posixpath.normpath(unquote(rootfile.get("full-path", "")))
            if rootfile is not None
            else ""
        )
        opf = self._read_xml(self._opf_path) if rootfile is not None else None
        if opf is None:
            raise BookFormatError("The EPUB package document could not be found.")

        self.title: str | None = None
        self.author: str | None = None
        metadata = _xml_find(opf, "metadata")
        cover_id = None
        if metadata is not None:
            for node in metadata:
                name = _local(node.tag)
                text = _clean("".join(node.itertext()))
                if name == "title" and text and self.title is None:
                    self.title = text
                elif name == "creator" and text and self.author is None:
                    self.author = text
                elif name == "meta" and node.get("name") == "cover":
                    cover_id = node.get("content")

        self._manifest: dict[str, dict[str, str]] = {}
        self._by_path: dict[str, dict[str, str]] = {}
        manifest = _xml_find(opf, "manifest")
        for item in _xml_children(manifest, "item") if manifest is not None else []:
            item_id, href = item.get("id"), item.get("href")
            if item_id and href:
                path, _ = _split_href(self._opf_path, href)
                entry = {
                    "path": path,
                    "type": item.get("media-type", ""),
                    "props": item.get("properties", ""),
                }
                self._manifest[item_id] = entry
                self._by_path.setdefault(path, entry)

        spine = _xml_find(opf, "spine")
        self._ncx_id = spine.get("toc") if spine is not None else None
        self.spine: list[str] = []
        refs = _xml_children(spine, "itemref") if spine is not None else []
        if len(refs) > MAX_ARCHIVE_ENTRIES:
            raise BookFormatError("The EPUB spine has too many entries.")
        for ref in refs:
            entry = self._manifest.get(ref.get("idref", ""))
            if entry is not None and entry["path"] not in self.spine:
                self.spine.append(entry["path"])
        if not self.spine:
            raise BookFormatError("The EPUB has no readable content.")
        self._spine_index = {path: i for i, path in enumerate(self.spine)}

        self._cover_id = cover_id

    @cached_property
    def cover(self) -> bytes | None:
        candidates = [self._manifest.get(self._cover_id or "")] + [
            m for m in self._manifest.values() if "cover-image" in m["props"].split()
        ]
        cover = next((c for c in candidates if c and c["type"].startswith("image/")), None)
        return self._read(cover["path"]) if cover else None

    def _check_encryption(self) -> None:
        enc = self._read_xml("META-INF/encryption.xml")
        if enc is None:
            return
        for node in enc.iter():
            if _local(node.tag) == "cipherreference":
                path, _ = _split_href("", node.get("URI", ""))
                if path in self._spine_index:
                    raise BookFormatError("This EPUB is DRM-protected and cannot be read.")

    def doc(self, spine_idx: int) -> tuple[list[str], dict[str, int]]:
        path = self.spine[spine_idx]
        if path not in self._docs:
            self._docs[path] = self._parse_doc(path)
        return self._docs[path]

    def _parse_doc(self, path: str) -> tuple[list[str], dict[str, int]]:
        entry = self._by_path.get(path)
        if entry and entry["type"].startswith("image/"):
            return [], {}
        raw = self._read(path)
        if raw is None:
            return [], {}
        root = _parse_xhtml(raw)
        body = _xml_find(root, "body") if root is not None else None
        if body is None:
            return [], {}
        out: list[str] = []
        marks: dict[str, int] = {}
        _note_anchors(body, out, marks)
        _walk(body, out, marks)
        self._chars += sum(len(p) for p in out)
        if self._chars > MAX_EXTRACTED_CHARS:
            raise BookFormatError("The EPUB contains too much text.")
        return out, marks

    def anchor_pos(self, spine_idx: int, anchor: str | None) -> int | None:
        return self.doc(spine_idx)[1].get(anchor)

    def markdown(self, locator: dict[str, Any]) -> str:
        start, end = int(locator["start_spine"]), int(locator["end_spine"])
        start_anchor, end_anchor = locator.get("start_anchor"), locator.get("end_anchor")
        last = end if end_anchor else end - 1
        paras: list[str] = []
        for s in range(max(start, 0), min(last, len(self.spine) - 1) + 1):
            doc, marks = self.doc(s)
            lo = marks.get(start_anchor, 0) if s == start else 0
            hi = marks.get(end_anchor, len(doc)) if s == end else len(doc)
            paras.extend(doc[lo:hi])
        return "\n\n".join(paras)

    def _toc_entries(self) -> list[tuple[str, int, str | None]]:
        nav = next((m for m in self._manifest.values() if "nav" in m["props"].split()), None)
        raw: list[tuple[str, str]] = []
        base = ""
        if nav is not None:
            base = nav["path"]
            raw = self._nav_entries(base)
        if not raw:
            ncx = self._manifest.get(self._ncx_id or "") or next(
                (m for m in self._manifest.values() if m["type"] == _NCX_MEDIA_TYPE), None
            )
            if ncx is not None:
                base = ncx["path"]
                raw = self._ncx_entries(base)
        entries = []
        for title, href in raw:
            path, frag = _split_href(base, href)
            if path in self._spine_index and title:
                entries.append((title, self._spine_index[path], frag))
        return entries

    def _nav_entries(self, path: str) -> list[tuple[str, str]]:
        data = self._read(path)
        root = _parse_xhtml(data) if data is not None else None
        if root is None:
            return []
        navs = [n for n in root.iter() if _local(n.tag) == "nav"]
        nav = next((n for n in navs if "toc" in _epub_types(n)), navs[0] if navs else None)
        top = _xml_find(nav, "ol") if nav is not None else None
        if (
            nav is not None
            and sum(1 for n in nav.iter() if _local(n.tag) == "li") > MAX_ARCHIVE_ENTRIES
        ):
            raise BookFormatError("The EPUB table of contents has too many entries.")
        entries = []
        for li in _xml_children(top, "li") if top is not None else []:
            link = next((c for c in li if _local(c.tag) in ("a", "span")), None)
            if link is None:
                continue
            anchor = (
                link
                if _local(link.tag) == "a"
                else next((n for n in link.iter() if _local(n.tag) == "a"), None)
            )
            if anchor is None or not anchor.get("href"):
                continue
            entries.append((_clean(_inline(link)), anchor.get("href", "")))
        return entries

    def _ncx_entries(self, path: str) -> list[tuple[str, str]]:
        raw = self._read(path)
        try:
            root = DET.fromstring(raw) if raw is not None else None
        except _XML_PARSE_ERRORS:
            return []
        nav_map = _xml_find(root, "navmap") if root is not None else None
        if (
            nav_map is not None
            and sum(1 for n in nav_map.iter() if _local(n.tag) == "navpoint") > MAX_ARCHIVE_ENTRIES
        ):
            raise BookFormatError("The EPUB table of contents has too many entries.")
        entries = []
        for point in _xml_children(nav_map, "navpoint") if nav_map is not None else []:
            label = _xml_find(point, "navlabel")
            content = next((c for c in point if _local(c.tag) == "content"), None)
            if label is None or content is None or not content.get("src"):
                continue
            entries.append((_clean("".join(label.itertext())), content.get("src", "")))
        return entries

    def chapter_ranges(self) -> list[tuple[str | None, dict[str, Any]]]:
        """(title, locator) per chapter; title is None for spine-fallback entries without a heading."""
        entries = self._toc_entries()
        if not entries:
            return self._spine_ranges()
        kept: list[tuple[str, int, str | None]] = []
        for title, spine_idx, anchor in entries:
            if kept:
                prev_spine, prev_anchor = kept[-1][1], kept[-1][2]
                if spine_idx < prev_spine:
                    continue
                if spine_idx == prev_spine:
                    pos = self.anchor_pos(spine_idx, anchor)
                    prev_pos = self.anchor_pos(spine_idx, prev_anchor) or 0
                    if pos is None or pos <= prev_pos:
                        continue
            if anchor and self.anchor_pos(spine_idx, anchor) is None:
                anchor = None
            kept.append((title, spine_idx, anchor))
        ranges = []
        for i, (title, spine_idx, anchor) in enumerate(kept):
            end_spine, end_anchor = (
                (kept[i + 1][1], kept[i + 1][2]) if i + 1 < len(kept) else (len(self.spine), None)
            )
            ranges.append((title, _locator(spine_idx, anchor, end_spine, end_anchor)))
        return ranges

    def _spine_ranges(self) -> list[tuple[str | None, dict[str, Any]]]:
        ranges = []
        for s in range(len(self.spine)):
            heading = next((p for p in self.doc(s)[0] if p.startswith("#")), None)
            title = heading.lstrip("#").strip() if heading else None
            ranges.append((title, _locator(s, None, s + 1, None)))
        return ranges


def _locator(
    start: int, start_anchor: str | None, end: int, end_anchor: str | None
) -> dict[str, Any]:
    return {
        "start_spine": start,
        "start_anchor": start_anchor,
        "end_spine": end,
        "end_anchor": end_anchor,
    }


def _flags(title: str, char_count: int, has_image: bool, min_chars: int) -> tuple[bool, bool]:
    """(default_selected, selectable)."""
    selectable = char_count >= min_chars or has_image
    return selectable and not is_front_or_back_matter(title), selectable


def _detect_pdf(data: bytes, min_chars: int) -> DetectedBook | None:
    try:
        reader = PdfReader(io.BytesIO(data))
        outline = reader.outline
        if len(outline) > MAX_ARCHIVE_ENTRIES:
            raise BookFormatError("The PDF outline has too many entries.")
        total = len(reader.pages)
        kept: list[tuple[str, int]] = []
        for item in outline:
            if isinstance(item, list):
                continue
            try:
                page = reader.get_destination_page_number(item)
            except (PyPdfError, ValueError, KeyError, TypeError, AttributeError):
                continue
            if not isinstance(page, int) or not 0 <= page < total:
                continue
            if kept and page <= kept[-1][1]:
                continue
            kept.append((_clean(str(item.title or "")), page))
        if len(kept) < 2:
            return None
        chapters: list[DetectedChapter] = []
        chars = 0
        for i, (title, first) in enumerate(kept):
            last = (kept[i + 1][1] if i + 1 < len(kept) else total) - 1
            pages = [reader.pages[n] for n in range(first, last + 1)]
            text = "\n\n".join(t for t in (p.extract_text().strip() for p in pages) if t)
            chars += len(text)
            if chars > MAX_EXTRACTED_CHARS:
                raise BookFormatError("The PDF contains too much text.")
            title = title or f"Section {i + 1}"
            has_image = any(page_has_image(p) for p in pages)
            default_selected, selectable = _flags(title, len(text), has_image, min_chars)
            chapters.append(
                DetectedChapter(
                    idx=i + 1,
                    title=title,
                    locator={"first_page": first, "last_page": last},
                    word_count=len(text.split()),
                    char_count=len(text),
                    default_selected=default_selected,
                    selectable=selectable,
                )
            )
        info = reader.metadata
        book_title = str(info.title) if info is not None and info.title else None
        author = str(info.author) if info is not None and info.author else None
    except BookFormatError:
        raise
    except (
        PyPdfError,
        ValueError,
        OSError,
        RecursionError,
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        raise BookFormatError(f"could not read PDF: {exc}") from exc
    return DetectedBook("pdf", book_title, author, None, chapters)


def detect(data: bytes, ext: str, *, min_chars: int) -> DetectedBook | None:
    if ext.lower() == ".pdf":
        return _detect_pdf(data, min_chars)
    if ext.lower() != ".epub":
        return None
    book = _Epub(data)
    chapters: list[DetectedChapter] = []
    for title, locator in book.chapter_ranges():
        text = book.markdown(locator)
        if not text:
            continue
        idx = len(chapters) + 1
        title = title or f"Section {idx}"
        default_selected, selectable = _flags(title, len(text), False, min_chars)
        chapters.append(
            DetectedChapter(
                idx=idx,
                title=title,
                locator=locator,
                word_count=len(text.split()),
                char_count=len(text),
                default_selected=default_selected,
                selectable=selectable,
            )
        )
    if len(chapters) < 2:
        return None
    return DetectedBook("epub", book.title, book.author, book.cover, chapters)


def epub_chapter_markdown(data: bytes, locator: dict[str, Any]) -> str:
    return _Epub(data).markdown(locator)


def epub_full_markdown(data: bytes) -> tuple[str, dict[str, Any]]:
    book = _Epub(data)
    text = book.markdown(_locator(0, None, len(book.spine), None))
    return text, {"title": book.title, "author": book.author}
