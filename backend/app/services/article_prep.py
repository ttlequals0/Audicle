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
# An image: ``![alt](url)`` -- dropped entirely (the alt text is rarely narratable).
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
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
# Collapse 3+ newlines to a single paragraph break -- shared by strip_chrome and
# strip_boilerplate.
_BLANK_RUN_RE = re.compile(r"\n{3,}")


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
    return _BLANK_RUN_RE.sub("\n\n", text).strip()


def strip_inline_markdown(text: str) -> str:
    """Reduce inline markdown to spoken text: drop images, keep link labels.

    Used on the raw-extraction fallback in the cleanup stage (when the LLM won't
    produce narration) so the narrator reads the link text, not the raw URL.
    Deliberately conservative -- only the two constructs that read badly aloud
    (``![alt](url)`` and ``[label](url)``); emphasis/code punctuation is left to
    the normalize stage, which already handles it, to avoid mangling prose that
    legitimately contains ``*`` or ``_``."""

    text = _IMAGE_RE.sub("", text)
    return _LINK_RE.sub(r"\1", text)


# --- deterministic boilerplate strip for the cleanup raw-extraction fallback --------
# Applied only when the LLM won't clean an article, to bin the obvious non-article cruft
# the extraction carries (ad markers, nav, dateline, tip/contact lines, subscribe).
# Conservative and cue-anchored so ordinary prose is not cut; line-oriented because the
# fallback input is paragraph-structured (one block per line). ``_SHORT_LINE`` caps the
# line-level patterns to chrome-sized lines: real body paragraphs (and a whole single-
# line article) run longer, so a cue appearing inside prose does not delete the paragraph.
_SHORT_LINE = r"(?=[^\n]{0,200}$)"
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_AD_MARKER_RE = re.compile(r"\b(?:REG AD|ADVERTISEMENT)\b")
_SKIP_NAV_RE = re.compile(
    rf"(?im)^{_SHORT_LINE}[^\n]*\b(?:jump|skip) to (?:main )?(?:content|navigation|search)\b[^\n]*$"
)
# A dateline line -- timezone-anchored and short so prose ("published a book") is safe.
_DATELINE_RE = re.compile(
    rf"(?im)^{_SHORT_LINE}[^\n]*\b(?:Published|Updated|Posted)\b"
    r"[^\n]*\b(?:UTC|GMT|E[SD]T|P[SD]T|BST|CET)\b[^\n]*$"
)
# An editorial-desk solicitation line -- specific phrases only, not generic prose like
# "get in touch" or "email us" that occurs in ordinary body text.
_TIPLINE_RE = re.compile(
    rf"(?im)^{_SHORT_LINE}[^\n]*\b(?:share it with us|got a (?:tip|story)|have a (?:story|tip)|"
    r"send us a (?:tip|story)|anonymity is available upon request)\b[^\n]*$"
)
_SUBSCRIBE_RE = re.compile(rf"(?im)^{_SHORT_LINE}[^\n]*\b(?:sign up|subscribe)\b[^\n]*\bnewsletter\b[^\n]*$")
_SPACE_RUN_RE = re.compile(r"[^\S\n]{2,}")


def strip_boilerplate(text: str) -> str:
    """Bin obvious non-article cruft so the cleanup raw-extraction fallback narrates the
    article body, not ad markers / nav / datelines / tip-lines / subscribe prompts.

    Conservative by design: ambiguous chrome (section labels, deks, plain bylines) is
    left in rather than risk cutting real prose. Reuses ``strip_inline_markdown`` for
    links/images. Intended for the paragraph-structured extraction on the fallback path,
    not general prose."""

    text = _HTML_TAG_RE.sub("", text)
    text = _AD_MARKER_RE.sub("", text)
    text = _SKIP_NAV_RE.sub("", text)
    text = _DATELINE_RE.sub("", text)
    text = _TIPLINE_RE.sub("", text)
    text = _SUBSCRIBE_RE.sub("", text)
    text = strip_inline_markdown(text)
    text = _SPACE_RUN_RE.sub(" ", text)
    return _BLANK_RUN_RE.sub("\n\n", text).strip()
