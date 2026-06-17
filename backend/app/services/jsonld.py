"""Shared JSON-LD article-body parsing.

Both extraction engines that receive raw page HTML -- the Firecrawl client and the
in-process direct fetcher -- read the publisher's declared ``articleBody`` length to
tell a chrome-padded teaser from a real article, so the parsing lives here once.
"""

from __future__ import annotations

import json
import re
from typing import Any

_LD_SCRIPT_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def _iter_ld_nodes(data: Any) -> Any:
    """Yield dict nodes from a parsed JSON-LD blob, flattening the ``@graph`` wrapper
    and top-level lists so an ``articleBody`` is found wherever the page puts it."""

    if isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                if isinstance(item, dict):
                    yield item
        yield data
    elif isinstance(data, list):
        for item in data:
            yield from _iter_ld_nodes(item)


def article_body_chars(raw_html: str) -> int | None:
    """Length of the longest JSON-LD ``articleBody`` the page declares, or None.

    This is the publisher's own article text, so it ignores the related-article and
    navigation chrome that can pad a scraped teaser past the floor. Never raises --
    the HTML and its embedded JSON are attacker-controlled."""

    if not raw_html:
        return None
    best: int | None = None
    for script in _LD_SCRIPT_RE.finditer(raw_html):
        try:
            data = json.loads(script.group(1).strip())
        except ValueError:
            continue
        for node in _iter_ld_nodes(data):
            body = node.get("articleBody")
            if isinstance(body, str):
                best = max(best or 0, len(body.strip()))
    return best
