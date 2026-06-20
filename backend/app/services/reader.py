"""Reader-proxy extraction engine (Jina Reader, https://r.jina.ai by default).

A bot-wall bypass for hosts that answer a direct scrape with a DataDome/PerimeterX
challenge -- e.g. wsj.com, which returns 401. The reader service fetches the article in
its own infrastructure and returns clean markdown, which the pipeline uses as-is: the
proxy already did the HTML->markdown step, so there is no trafilatura pass. Selected per
host via the ``reader`` source-fallback strategy and run inside the bypass cascade, like
FlareSolverr and the web archive.

The proxy endpoint (``READER_PROXY_TEMPLATE``) and an optional ``READER_API_KEY`` are
operator config: the keyless public endpoint is rate limited, so a free key or a self-host
keeps it usable under load. The outbound fetch is SSRF-pinned to a validated public IP for
the initial request and every redirect hop, and size-capped, via ``pinned_fetch``.
"""

from __future__ import annotations

from app.config import Settings
from app.services import pinned_fetch
from app.services.extraction_types import ExtractionPermanentError, ExtractionResult
from app.services.html_markdown import MAX_HTML_CHARS

# Jina Reader prefixes the article with a small metadata header ("Title: ...", "URL
# Source: ...", then "Markdown Content:" before the body). Split the body off this marker
# and lift the title; if the marker is absent (a different reader proxy), use the raw text.
_CONTENT_MARKER = "Markdown Content:"
_TITLE_LABEL = "Title:"
_READER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_ACCEPT = "text/plain, text/markdown, */*"


async def fetch(article_url: str, settings: Settings) -> ExtractionResult:
    """Fetch ``article_url`` through the configured reader proxy and return its markdown as
    an ``ExtractionResult``. Transient failures are retried with the shared extraction
    policy; 4xx/SSRF blocks are permanent. The caller validates length."""

    reader_url = _build_reader_url(settings.READER_PROXY_TEMPLATE, article_url)
    headers = {"User-Agent": _READER_UA, "Accept": _ACCEPT}
    if settings.READER_API_KEY:
        headers["Authorization"] = f"Bearer {settings.READER_API_KEY}"
    body = await pinned_fetch.get_text_retrying(
        reader_url,
        settings,
        headers=headers,
        max_bytes=MAX_HTML_CHARS,
        timeout_seconds=settings.FIRECRAWL_TIMEOUT_SECONDS,
    )
    return _parse(body)


def _build_reader_url(template: str, article_url: str) -> str:
    """Apply the proxy template to the article URL. A misconfigured template (missing
    ``{url}`` or with a stray placeholder) raises ExtractionPermanentError -- a typed error
    the cascade catches and skips, instead of an uncaught KeyError/ValueError that would
    crash the job. (The article URL itself is a substituted value, so its braces are safe.)"""

    if "{url}" not in template:
        raise ExtractionPermanentError("READER_PROXY_TEMPLATE must contain {url}.")
    try:
        return template.format(url=article_url)
    except (KeyError, IndexError, ValueError) as exc:
        raise ExtractionPermanentError(f"READER_PROXY_TEMPLATE is malformed: {exc}") from exc


def _parse(body: str) -> ExtractionResult:
    """Split the reader proxy's metadata header ("Title:" ... "Markdown Content:") off the
    body. Without the marker (a non-Jina proxy) the whole text is treated as the markdown."""

    head, marker, tail = body.partition(_CONTENT_MARKER)
    markdown = tail if marker else body
    metadata: dict[str, str] = {}
    for line in head[:2000].splitlines():
        if line.startswith(_TITLE_LABEL):
            title = line[len(_TITLE_LABEL) :].strip()
            if title:
                metadata["title"] = title
            break
    return ExtractionResult(markdown=markdown.strip(), metadata=metadata)
