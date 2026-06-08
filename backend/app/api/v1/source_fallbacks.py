"""``/api/v1/source-fallbacks`` -- operator config for paywall extraction fallbacks.

A list of hosts routed through a bypass strategy (built-in Googlebot fetch, Freedium,
a custom reader-proxy template, or reject), with a global default strategy. Stored as a
single JSON blob (``source_fallbacks_store``) and merged over the built-in rules at
extraction time. Admin-gated by the ``/api/v1`` router group.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from app.config import Settings, get_settings
from app.core import database
from app.services import source_fallbacks, source_fallbacks_store

router = APIRouter(tags=["source-fallbacks"])

# Human labels for the UI dropdown. The list is derived from PROXY_KEYS (the single
# source of truth) so it can't drift; a new key without a label fails loudly at import.
_PROXY_LABELS = {
    "googlebot": "Built-in (Googlebot fetch)",
    "freedium": "Freedium (Medium)",
    "custom": "Custom URL template",
    "none": "None / reject",
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


def _response(config: dict[str, Any]) -> dict[str, Any]:
    return {**config, "available_proxies": _AVAILABLE_PROXIES, "builtin": _BUILTIN}


@router.get("/source-fallbacks", summary="Read paywall extraction fallback config")
def read_source_fallbacks(
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    with database.connection(settings.DATA_DIR) as conn:
        return _response(source_fallbacks_store.load(conn))


@router.put("/source-fallbacks", summary="Replace paywall extraction fallback config")
def write_source_fallbacks(
    body: dict[str, Any],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    try:
        with database.connection(settings.DATA_DIR) as conn:
            saved = source_fallbacks_store.save(conn, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _response(saved)
