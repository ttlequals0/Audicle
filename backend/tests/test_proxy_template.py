from __future__ import annotations

import pytest
from app.services.extraction_types import ExtractionPermanentError
from app.services.proxy_template import build_proxy_url


def test_build_proxy_url_wraps_article() -> None:
    assert (
        build_proxy_url("https://r.jina.ai/{url}", "https://x.com/a", "READER_PROXY_TEMPLATE")
        == "https://r.jina.ai/https://x.com/a"
    )


def test_build_proxy_url_rejects_missing_placeholder_naming_the_setting() -> None:
    with pytest.raises(ExtractionPermanentError, match="WALLHOP_PROXY_TEMPLATE must contain"):
        build_proxy_url("https://wallhop.io/raw/", "https://x.com/a", "WALLHOP_PROXY_TEMPLATE")


def test_build_proxy_url_rejects_stray_placeholder() -> None:
    # A template with an unknown field must raise the typed error, not a bare KeyError that
    # would escape the cascade's `except ExtractionError` and crash the job.
    with pytest.raises(ExtractionPermanentError, match="malformed"):
        build_proxy_url(
            "https://r.jina.ai/{url}?k={key}", "https://x.com/a", "READER_PROXY_TEMPLATE"
        )
