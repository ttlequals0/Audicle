from __future__ import annotations

import json

from app.services import arc_extractor

_LONG = "Lorem ipsum dolor sit amet consectetur adipiscing elit. " * 12


def _page(elements: list[dict]) -> str:
    blob = json.dumps({"content_elements": elements})
    return f'<html><body><script type="application/json">{blob}</script></body></html>'


def test_non_arc_page_returns_none() -> None:
    assert arc_extractor.extract_body("<html><p>just a normal page</p></html>") is None
    assert arc_extractor.extract_body("") is None


def test_extracts_text_headings_lists_quotes() -> None:
    html = _page(
        [
            {"type": "text", "content": "<p>First <b>paragraph</b> here.</p>"},
            {"type": "header", "level": 2, "content": "A Heading"},
            {"type": "list", "items": [{"content": "one"}, {"content": "two"}]},
            {"type": "blockquote", "content": "a quote"},
            {"type": "image", "url": "x.jpg"},  # ignored
        ]
    )
    md = arc_extractor.extract_body(html)
    assert md is not None
    assert "First paragraph here." in md
    assert "## A Heading" in md
    assert "- one" in md and "- two" in md
    assert "> a quote" in md
    assert "x.jpg" not in md


def test_strips_inline_html_and_entities() -> None:
    html = _page([{"type": "text", "content": "A &amp; B <a href='/x'>link</a> end"}])
    assert arc_extractor.extract_body(html) == "A & B link end"


def test_bracket_in_content_does_not_truncate_array() -> None:
    # A ']' inside a content string must not end the content_elements scan early.
    html = _page(
        [
            {"type": "text", "content": "array notation a[0] and b]c here"},
            {"type": "text", "content": _LONG},
        ]
    )
    md = arc_extractor.extract_body(html)
    assert md is not None
    assert "array notation a[0] and b]c here" in md
    assert "Lorem ipsum" in md


def test_malformed_json_returns_none() -> None:
    html = '<html>"content_elements": [ {not valid json ]</html>'
    assert arc_extractor.extract_body(html) is None
