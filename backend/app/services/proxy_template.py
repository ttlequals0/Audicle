"""Apply an operator-configured proxy URL template (reader, wallhop) to an article URL."""

from __future__ import annotations

from app.services.extraction_types import ExtractionPermanentError


def build_proxy_url(template: str, article_url: str, setting: str) -> str:
    """Substitute ``article_url`` into ``template``. A template missing ``{url}`` or
    carrying a stray placeholder raises ExtractionPermanentError, a typed error the
    cascade skips instead of a KeyError/ValueError that would crash the job. ``setting``
    names the env key in the message. (The article URL is a substituted value, so its
    own braces are safe.)"""

    if "{url}" not in template:
        raise ExtractionPermanentError(f"{setting} must contain {{url}}.")
    try:
        return template.format(url=article_url)
    except (KeyError, IndexError, ValueError) as exc:
        raise ExtractionPermanentError(f"{setting} is malformed: {exc}") from exc
