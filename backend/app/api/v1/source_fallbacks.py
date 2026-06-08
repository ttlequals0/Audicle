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
from app.services import runtime_settings, source_fallbacks, source_fallbacks_store

router = APIRouter(tags=["source-fallbacks"])

# Human labels for the UI dropdown. The list is derived from PROXY_KEYS (the single
# source of truth) so it can't drift; a new key without a label fails loudly at import.
_PROXY_LABELS = {
    "googlebot": "Built-in (Googlebot fetch)",
    "freedium": "Freedium (Medium)",
    "custom": "Custom URL template",
    "none": "None / reject",
    "flaresolverr": "FlareSolverr (browser; hard blocks)",
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
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    with database.connection(settings.DATA_DIR) as conn:
        return _masked_response(source_fallbacks_store.load(conn))


@router.put("/source-fallbacks", summary="Replace paywall extraction fallback config")
def write_source_fallbacks(
    body: dict[str, Any],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    try:
        with database.connection(settings.DATA_DIR) as conn:
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
