"""``/api/v1/source-fallbacks`` -- operator config for paywall extraction fallbacks.

A list of hosts routed through a bypass strategy (built-in Googlebot fetch, Freedium,
a custom reader-proxy template, or reject), with a global default strategy. Stored as a
single JSON blob (``source_fallbacks_store``) and merged over the built-in rules at
extraction time. Admin-gated by the ``/api/v1`` router group.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AnyHttpUrl

from app.api.deps import get_conn
from app.config import Settings, get_settings
from app.services import extraction, runtime_settings, source_fallbacks, source_fallbacks_store

logger = logging.getLogger("app.api.v1.source_fallbacks")

router = APIRouter(tags=["source-fallbacks"])

# Human labels for the UI dropdown. The list is derived from PROXY_KEYS (the single
# source of truth) so it can't drift; a new key without a label fails loudly at import.
_PROXY_LABELS = {
    "googlebot": "Built-in (Googlebot fetch)",
    "freedium": "Freedium (Medium)",
    "custom": "Custom URL template",
    "none": "None / reject",
    "flaresolverr": "FlareSolverr (browser; hard blocks)",
    "archive": "Archive (Wayback / archive.today)",
    "render": "Render (headful browser; clicks expand gates)",
}
_AVAILABLE_PROXIES = [
    {"key": key, "label": _PROXY_LABELS[key]} for key in source_fallbacks.PROXY_KEYS
]
# BUILTIN is static, so flatten its hosts once at import (mirrors _AVAILABLE_PROXIES).
_BUILTIN = [
    {"host": suffix, "proxy": rule.proxy}
    for rule in source_fallbacks.BUILTIN
    for suffix in rule.host_suffixes
]


def _masked_response(config: dict[str, Any]) -> dict[str, Any]:
    # Cookies are session secrets: never echo them. The sentinel just signals "a
    # cookie jar is set" so the UI can show the field as configured.
    rules = [
        {**rule, "cookies": runtime_settings.MASK_SENTINEL if rule.get("cookies") else ""}
        for rule in config.get("rules", [])
    ]
    return {**config, "rules": rules, "available_proxies": _AVAILABLE_PROXIES, "builtin": _BUILTIN}


@router.get("/source-fallbacks", summary="Read paywall extraction fallback config")
def read_source_fallbacks(
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> dict[str, Any]:
    return _masked_response(source_fallbacks_store.load(conn))


@router.put("/source-fallbacks", summary="Replace paywall extraction fallback config")
def write_source_fallbacks(
    body: dict[str, Any],
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> dict[str, Any]:
    try:
        # A rule that sends back the mask sentinel keeps its stored cookies (the UI
        # never saw the real value); any other value (new cookies, or "" to clear)
        # is taken as-is.
        stored = {
            rule["host"]: rule.get("cookies", "")
            for rule in source_fallbacks_store.load(conn).get("rules", [])
        }
        rules_in = body.get("rules")
        for rule in rules_in if isinstance(rules_in, list) else []:
            if isinstance(rule, dict) and rule.get("cookies") == runtime_settings.MASK_SENTINEL:
                rule["cookies"] = stored.get(str(rule.get("host", "")).strip().lower(), "")
        saved = source_fallbacks_store.save(conn, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _masked_response(saved)


@router.post("/source-fallbacks/test", summary="Test the bypass config against one URL")
async def test_source_fallback(
    body: dict[str, Any],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Run the stored bypass config against ``url`` once and report what came back, so
    the operator can confirm a rule (and its cookie jar) actually fetches the article.
    Uses the real stored cookies but never echoes them -- only a char count, the
    strategy that matched, and a short text sample of the extracted article."""

    url = str(body.get("url", "")).strip()
    if not url:
        raise HTTPException(status_code=400, detail="A url is required.")
    # Reject non-http(s) schemes (file://, gopher://, ...) before handing the URL to
    # Firecrawl/the solver -- the same AnyHttpUrl guard the /submit endpoint uses.
    try:
        AnyHttpUrl(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="A valid http(s) URL is required.") from exc
    # Apply the runtime-settings overlay (like /health/ready) so the test reflects the
    # operator's UI-set config -- the render sidecar URL in particular is overlay-only,
    # so without this the "test a URL" button could never exercise the render strategy.
    # A DB hiccup must not 500 the test; fall back to the base settings already bound.
    with contextlib.suppress(Exception):
        settings = runtime_settings.overlay(settings)
    registry = source_fallbacks_store.load_registry(settings)
    matched = source_fallbacks.match(url, registry)
    strategy = matched.proxy if matched is not None else None
    try:
        result = await extraction.extract(url, settings, registry)
    except extraction.ExtractionError as exc:
        # Log the specific reason server-side; the response carries a fixed message
        # rather than the exception text so internal detail can't leak to the client
        # (CodeQL py/stack-trace-exposure). The operator reads the reason from logs.
        logger.warning(
            "Source-fallback test extraction failed",
            extra={
                "event": "source_fallback_test_failed",
                "error_class": type(exc).__name__,
                "error": str(exc),
            },
        )
        return {
            "ok": False,
            "chars": 0,
            "strategy": strategy,
            "detail": "Extraction failed for this URL; see server logs for the reason.",
            "sample": "",
        }
    return {
        "ok": True,
        "chars": len(result.markdown),
        "strategy": strategy,
        "title": result.metadata.get("title"),
        "sample": result.markdown[:300],
    }
