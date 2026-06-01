"""Deterministic article-chrome stripping, run on Firecrawl markdown before the
LLM cleanup windows.

Firecrawl's ``onlyMainContent`` keeps wiki-style page furniture that lives inside
the main content: a table of contents, ``[edit]`` section links, citation
superscripts, and the trailing link-list sections (See also, References, External
links, ...). On a Wikipedia page the first cleanup window is otherwise dominated
by this chrome with little prose, which is what pushes the model into a
conversational reply. Removing it deterministically -- before windowing -- keeps
each window article-shaped and trims footer link dumps the narrator should never
read.

Conservative by design: only whole-line markdown structures and known appendix
headings are touched, so ordinary prose (a sentence that happens to mention
"references") is never altered.
"""

from __future__ import annotations

import re

# Appendix / navigation sections removed whole (heading through the next heading
# of the same or higher level). "Contents" / "Table of contents" is the TOC.
_DROP_SECTIONS: frozenset[str] = frozenset(
    {
        "contents",
        "table of contents",
        "see also",
        "references",
        "external links",
        "notes",
        "footnotes",
        "further reading",
        "citations",
        "bibliography",
        "sources",
        "works cited",
    }
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_EDIT_LINK_RE = re.compile(r"\[\s*edit\s*\]\([^)]*\)", re.IGNORECASE)
_EDIT_BARE_RE = re.compile(r"\[\s*edit\s*\]", re.IGNORECASE)
# Wikipedia renders a ref marker as a link whose visible text is a bracketed
# number, often backslash-escaped: ``[\[1\]](#cite_note-1)``.
_CITE_LINK_RE = re.compile(r"\[\\?\[\d{1,3}\\?\]\]\([^)]*\)")
# A bare ``[12]`` superscript -- but not a real markdown link label ``[12](url)``.
_CITE_BARE_RE = re.compile(r"\[\d{1,3}\](?!\()")
# A table-of-contents entry: a list item whose only content is an anchor link.
_TOC_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s*\[[^\]]+\]\(#[^)]*\)\s*$")
_JUMP_NAV_RE = re.compile(r"^\s*Jump to (?:content|navigation|search)\s*$", re.IGNORECASE)


def _normalize_heading(text: str) -> str:
    """Heading text reduced to lowercase alphanumerics for appendix matching."""

    text = _EDIT_LINK_RE.sub("", text)
    text = _EDIT_BARE_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)  # [label](url) -> label
    text = re.sub(r"[^a-z0-9 ]", "", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _drop_appendix_sections(lines: list[str]) -> list[str]:
    """Remove each appendix/TOC section: its heading through the line before the
    next heading of the same or higher level (or end of document)."""

    headings = [
        (idx, len(m.group(1)), _normalize_heading(m.group(2)))
        for idx, line in enumerate(lines)
        if (m := _HEADING_RE.match(line))
    ]
    remove = [False] * len(lines)
    for pos, (idx, level, title) in enumerate(headings):
        if title not in _DROP_SECTIONS:
            continue
        end = len(lines)
        for later_idx, later_level, _ in headings[pos + 1 :]:
            if later_level <= level:
                end = later_idx
                break
        for x in range(idx, end):
            remove[x] = True
    return [line for x, line in enumerate(lines) if not remove[x]]


def strip_chrome(markdown: str) -> str:
    """Strip TOC, appendix link-lists, ``[edit]`` markers, and citation
    superscripts from Firecrawl markdown. Returns cleaned markdown."""

    if not markdown:
        return markdown
    lines = _drop_appendix_sections(markdown.split("\n"))
    lines = [
        line
        for line in lines
        if not _TOC_ITEM_RE.match(line) and not _JUMP_NAV_RE.match(line)
    ]
    text = "\n".join(lines)
    text = _CITE_LINK_RE.sub("", text)
    text = _EDIT_LINK_RE.sub("", text)
    text = _EDIT_BARE_RE.sub("", text)
    text = _CITE_BARE_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
