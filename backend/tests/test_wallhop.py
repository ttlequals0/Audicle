from __future__ import annotations

from pathlib import Path

import pytest
from app.config import get_settings
from app.services import direct_fetch, wallhop
from app.services.extraction_types import ExtractionPermanentError, ExtractionResult

_ARTICLE = "https://www.404media.co/some-slug/"


async def test_fetch_wraps_article_and_uses_direct_fetch_with_teaser_detection(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    async def _fake_fetch(url, settings, *, detect_teaser=False, headers=None):
        captured.update(url=url, detect_teaser=detect_teaser)
        return ExtractionResult(
            markdown="# Title\n\nbody", metadata={"title": "Title"}, article_chars=1234
        )

    monkeypatch.setattr(direct_fetch, "fetch", _fake_fetch)
    result = await wallhop.fetch(_ARTICLE, get_settings())

    assert captured["url"] == f"https://wallhop.io/raw/{_ARTICLE}"
    assert captured["detect_teaser"] is True
    assert result.article_chars == 1234
    # The wallhop URL never reaches the result metadata.
    assert "wallhop" not in str(result.metadata)


async def test_fetch_rejects_template_without_placeholder(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WALLHOP_PROXY_TEMPLATE", "https://wallhop.io/raw/")
    get_settings.cache_clear()
    with pytest.raises(ExtractionPermanentError, match="must contain"):
        await wallhop.fetch(_ARTICLE, get_settings())


async def test_fetch_rejects_stray_placeholder(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WALLHOP_PROXY_TEMPLATE", "https://wallhop.io/raw/{url}?k={key}")
    get_settings.cache_clear()
    with pytest.raises(ExtractionPermanentError, match="malformed"):
        await wallhop.fetch(_ARTICLE, get_settings())
