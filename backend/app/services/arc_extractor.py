"""Arc XP / Fusion static body extractor (0.31.0).

Some publishers (Arc XP / Fusion CMS -- multiple Crain's titles, others) return a
short teaser in the visible DOM while the full article body sits untouched in the
page's ``content_elements`` JSON. trafilatura only reads the visible DOM and misses
it. This pulls the body straight out of the static HTML, ahead of the browser/
archive fallbacks, gated on the Arc signature so non-Arc pages are unaffected.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re

logger = logging.getLogger("app.services.arc_extractor")

_TAG = re.compile(r"<[^>]+>")


def extract_body(html: str) -> str | None:
    """Return the article body as markdown if ``html`` is an Arc/Fusion page with a
    parseable ``content_elements`` array, else None. Never raises."""

    # Cap the scan like html_markdown does (this path doesn't go through it), so a
    # pathologically large page can't burn CPU on the bracket walk.
    if not html or len(html) > 8_000_000 or '"content_elements"' not in html:
        return None
    raw = _extract_array(html, "content_elements")
    if raw is None:
        return None
    try:
        elements = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(elements, list):
        return None
    parts = [md for el in elements if (md := _element_md(el))]
    body = "\n\n".join(parts).strip()
    return body or None


def _extract_array(html: str, key: str) -> str | None:
    """Slice out the JSON array following ``"key"`` with a brace/bracket-balanced,
    string-aware scan (so embedded ``]`` inside content strings don't end it early)."""

    idx = html.find(f'"{key}"')
    if idx == -1:
        return None
    start = html.find("[", idx)
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(html)):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return html[start : i + 1]
    return None


def _element_md(el: object) -> str | None:
    if not isinstance(el, dict):
        return None
    etype = el.get("type")
    if etype == "text":
        return _strip(el.get("content", "")) or None
    if etype in ("header", "heading"):
        level = el.get("level", 2)
        try:
            level = min(max(int(level), 1), 6)
        except (TypeError, ValueError):
            level = 2
        text = _strip(el.get("content", ""))
        return f"{'#' * level} {text}" if text else None
    if etype == "list":
        items = el.get("items", [])
        lines = []
        for it in items if isinstance(items, list) else []:
            text = _strip(it.get("content", "")) if isinstance(it, dict) else _strip(str(it))
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines) or None
    if etype in ("blockquote", "quote"):
        text = _strip(el.get("content", ""))
        return f"> {text}" if text else None
    return None


def _strip(fragment: object) -> str:
    if not isinstance(fragment, str):
        return ""
    return html_lib.unescape(_TAG.sub("", fragment)).strip()
