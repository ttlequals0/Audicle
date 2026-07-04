"""Shared HTML -> article-markdown conversion.

Both fetch engines that receive raw HTML -- the FlareSolverr solver and the archive
fallback -- turn it into article markdown plus best-effort title/author/og:image
metadata here, rather than each carrying its own copy of the trafilatura call.
"""

from __future__ import annotations

import logging
import re
from itertools import takewhile
from typing import Any

import lxml.html
import trafilatura

from app.config import Settings

logger = logging.getLogger("app.services.html_markdown")

# Cap raw HTML before lxml builds a DOM (several times the source size in memory) so a
# pathologically large, attacker-controlled page can't OOM the worker. No real article
# is anywhere near this; the artwork path caps downloads for the same reason.
MAX_HTML_CHARS = 8_000_000

# Block elements whose text makes up an article body. Used by the <article> fallback
# below when trafilatura selects the wrong node (e.g. a "most popular" sidebar rail).
_ARTICLE_BLOCK_TAGS = ("p", "h1", "h2", "h3", "h4", "blockquote", "li")
# Floor for accepting the <article> fallback -- one source of truth with the extraction
# floor the pipeline enforces on the result.
_ARTICLE_MIN_CHARS = Settings.model_fields["MIN_EXTRACTION_CHARS"].default
_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _largest_article(html: str) -> lxml.html.HtmlElement | None:
    """The page's largest ``<article>`` element by text length, or ``None``. The recall
    net for pages where trafilatura's density heuristic picks a non-article node (a
    related-stories rail is often denser than the prose)."""

    try:
        tree = lxml.html.fromstring(html)
    except Exception:  # adversarial/malformed HTML; the fallback is best-effort
        return None
    articles = tree.xpath("//article")
    if not articles:
        return None
    return max(articles, key=lambda node: len(node.text_content()))


def _article_block_markdown(article: lxml.html.HtmlElement) -> str:
    """The article element's block text as ``\\n\\n``-joined paragraph markdown. Nested
    block elements are skipped so a quote's inner paragraph is not emitted twice."""

    parts: list[str] = []
    for el in article.iter(*_ARTICLE_BLOCK_TAGS):
        ancestors = takewhile(lambda node: node is not article, el.iterancestors())
        if any(node.tag in _ARTICLE_BLOCK_TAGS for node in ancestors):
            continue  # nested block; its text is already in the ancestor
        text = " ".join(el.text_content().split())
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


def _trafilatura_missed_article(trafilatura_md: str, article_md: str) -> bool:
    """True when trafilatura's output is not contained in the ``<article>`` text -- i.e.
    it extracted something outside the article body. Both sides are reduced to bare
    alphanumerics (punctuation AND whitespace dropped) so markdown formatting and a
    minified page's separator-less ``text_content()`` compare equal. Samples the START
    and the MIDDLE of trafilatura's text: a page's title/byline can live in a ``<header>``
    outside ``<article>``, so the opening may legitimately not match even when the body
    did -- if EITHER sample is inside the article, trafilatura hit the body (not missed)."""

    trafi = _ALNUM_RE.sub("", trafilatura_md.lower())
    article = _ALNUM_RE.sub("", article_md.lower())
    if len(trafi) < 40 or not article:
        return False
    mid = len(trafi) // 2
    probes = (trafi[:200], trafi[mid : mid + 200])
    return all(probe not in article for probe in probes)


def html_to_markdown(html: str) -> tuple[str, dict[str, Any]]:
    """Extract the main article body from raw HTML as markdown, plus best-effort
    title/author/og:image metadata mapped into the same keys the finalize and artwork
    stages already read from Firecrawl. Returns ``("", {})`` when there is no
    extractable article. Never raises -- the HTML is attacker-controlled."""

    if not html.strip():
        return "", {}
    if len(html) > MAX_HTML_CHARS:
        logger.warning(
            "HTML exceeds the size cap; skipping",
            extra={"event": "html_oversize", "chars": len(html)},
        )
        return "", {}
    try:
        markdown = (
            trafilatura.extract(
                html, output_format="markdown", include_comments=False, include_tables=True
            )
            or ""
        )
        meta = trafilatura.extract_metadata(html)
    except Exception:  # adversarial HTML; never fail extraction on a parse error
        logger.warning("trafilatura could not parse the HTML", extra={"event": "html_parse_error"})
        return "", {}
    # Recall net: fall back to the <article> element's block text only when trafilatura
    # both selected a node outside the article (its output isn't inside the <article>)
    # AND that <article> holds more text than it returned. The size guard stops a decoy
    # <article> (a comment or promo card) from downgrading a good long extraction on a
    # page whose body isn't <article>-wrapped. Assembled only when the guards pass.
    article = _largest_article(html)
    if article is not None:
        article_text = article.text_content()
        if (
            len(article_text) >= _ARTICLE_MIN_CHARS
            and len(article_text) > len(markdown)
            and _trafilatura_missed_article(markdown, article_text)
        ):
            article_md = _article_block_markdown(article)
            if len(article_md) >= _ARTICLE_MIN_CHARS:
                logger.info(
                    "trafilatura missed the article body; using <article> element",
                    extra={
                        "event": "extract_article_fallback",
                        "trafilatura_chars": len(markdown),
                        "article_chars": len(article_md),
                    },
                )
                markdown = article_md
    metadata: dict[str, Any] = {}
    if meta is not None:
        if getattr(meta, "title", None):
            metadata["title"] = meta.title
        if getattr(meta, "author", None):
            metadata["author"] = meta.author
        if getattr(meta, "image", None):
            metadata["ogImage"] = meta.image  # the key artwork._extract_og_image reads first
    return markdown.strip(), metadata
