from __future__ import annotations

from pathlib import Path

import pytest
from app.config import get_settings
from app.services import pinned_fetch, reader
from app.services.extraction_types import ExtractionPermanentError

# A representative Jina Reader response: a small metadata header, then the article body
# after the "Markdown Content:" marker.
_JINA_BODY = (
    "Title: Jane Street Seizes the AI Spotlight\n"
    "URL Source: https://www.wsj.com/tech/ai/jane-street\n"
    "Published Time: 2026-06-20T01:00:00.000Z\n"
    "Markdown Content:\n"
    "# Jane Street Seizes the AI Spotlight\n\n"
    "The secretive trading firm has become an unlikely AI powerhouse.\n"
)


def test_parse_splits_header_from_body() -> None:
    result = reader._parse(_JINA_BODY)
    assert result.metadata["title"] == "Jane Street Seizes the AI Spotlight"
    assert result.markdown.startswith("# Jane Street Seizes the AI Spotlight")
    # The header lines must not leak into the narration body.
    assert "URL Source:" not in result.markdown
    assert "Markdown Content:" not in result.markdown


def test_parse_without_marker_keeps_raw_body() -> None:
    """A reader proxy that emits no Jina header just yields its text as the markdown."""

    result = reader._parse("# A plain article\n\nNo header here.")
    assert result.markdown == "# A plain article\n\nNo header here."
    assert result.metadata == {}


async def test_fetch_wraps_article_in_proxy_template(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    async def _fake_get_text(url, settings, *, headers, max_bytes, timeout_seconds):
        captured["url"] = url
        captured["headers"] = headers
        return _JINA_BODY

    monkeypatch.setattr(pinned_fetch, "get_text", _fake_get_text)
    result = await reader.fetch("https://www.wsj.com/a", get_settings())

    # The article URL is wrapped with the default Jina template; no auth header by default.
    assert captured["url"] == "https://r.jina.ai/https://www.wsj.com/a"
    assert "Authorization" not in captured["headers"]
    assert result.metadata["title"] == "Jane Street Seizes the AI Spotlight"
    assert "unlikely AI powerhouse" in result.markdown


def test_build_reader_url_wraps_article() -> None:
    assert (
        reader._build_reader_url("https://r.jina.ai/{url}", "https://x.com/a")
        == "https://r.jina.ai/https://x.com/a"
    )


def test_build_reader_url_rejects_missing_placeholder() -> None:
    with pytest.raises(ExtractionPermanentError, match="must contain"):
        reader._build_reader_url("https://r.jina.ai/", "https://x.com/a")


def test_build_reader_url_rejects_stray_placeholder() -> None:
    # A template with an unknown field must raise the typed error, not a bare KeyError that
    # would escape the cascade's `except ExtractionError` and crash the job.
    with pytest.raises(ExtractionPermanentError, match="malformed"):
        reader._build_reader_url("https://r.jina.ai/{url}?k={key}", "https://x.com/a")


async def test_fetch_sends_api_key_when_configured(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("READER_API_KEY", "jina_secret")
    get_settings.cache_clear()
    captured: dict[str, object] = {}

    async def _fake_get_text(url, settings, *, headers, max_bytes, timeout_seconds):
        captured["headers"] = headers
        return _JINA_BODY

    monkeypatch.setattr(pinned_fetch, "get_text", _fake_get_text)
    await reader.fetch("https://www.wsj.com/a", get_settings())

    assert captured["headers"]["Authorization"] == "Bearer jina_secret"
