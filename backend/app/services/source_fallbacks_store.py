"""Operator config for source-extraction paywall fallbacks.

Stored as one JSON blob in the ``settings`` table (key ``source_fallbacks``):
``{default_proxy, min_chars, rules: [{host, proxy, custom_template}]}``. A rule's
``proxy`` may be "" -- meaning "use the global default" -- so the operator's default
proxy can change without re-pinning every row. Built-in rules live in
``source_fallbacks.BUILTIN``; operator rules layer on top at extraction time via
``source_fallbacks.build_registry``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from app.services import settings_store
from app.services.source_fallbacks import PROXY_KEYS

_KEY = "source_fallbacks"
_DEFAULT_PROXY = "googlebot"
_DEFAULT_MIN_CHARS = 3000
# A bare host after lowercasing: letters/digits/dots/hyphens only (no scheme, path,
# port, or whitespace). Guards against an operator pasting a full article URL.
_HOST_RE = re.compile(r"^[a-z0-9.-]+$")


def _defaults() -> dict[str, Any]:
    return {"default_proxy": _DEFAULT_PROXY, "min_chars": _DEFAULT_MIN_CHARS, "rules": []}


def _normalize_rule(raw: dict[str, Any]) -> dict[str, str]:
    return {
        "host": str(raw.get("host", "")).strip().lower(),
        # "" -> use the global default (resolved by build_registry).
        "proxy": str(raw.get("proxy") or "").strip(),
        "custom_template": str(raw.get("custom_template", "")).strip(),
    }


def _validate_custom_template(template: str) -> None:
    """Reject a custom proxy template that won't render at extraction time.

    Dry-runs ``.format(url=...)`` so a template with a stray brace or any placeholder
    other than ``{url}`` (e.g. ``.../{url}?k={key}``) fails at save time with a 400
    instead of raising KeyError/ValueError out of ``extract()`` while narrating.
    """

    if not template.startswith(("http://", "https://")):
        raise ValueError("a custom proxy needs an http(s) template containing {url}")
    try:
        rendered = template.format(url="https://example.test/probe")
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError("custom template must contain only the {url} placeholder") from exc
    if rendered == template:  # {url} absent, so nothing was substituted
        raise ValueError("a custom proxy needs an http(s) template containing {url}")


def _validate(config: dict[str, Any]) -> dict[str, Any]:
    default_proxy = config.get("default_proxy") or _DEFAULT_PROXY
    if default_proxy not in PROXY_KEYS:
        raise ValueError(f"default_proxy must be one of {list(PROXY_KEYS)}")
    if default_proxy == "custom":
        # There is no global template field, so a custom default can never render.
        raise ValueError("default_proxy cannot be 'custom'; set custom per-site instead")
    if default_proxy == "flaresolverr":
        # FlareSolverr runs a real browser; as a global default it would route every
        # below-floor scrape through an expensive solve, defeating the challenge gate.
        # It is a per-host remedy for hosts that hard-block the scraper IP.
        raise ValueError("default_proxy cannot be 'flaresolverr'; set it per-site instead")
    raw_min = config.get("min_chars", _DEFAULT_MIN_CHARS)
    if isinstance(raw_min, bool):  # bool is an int subclass; True would coerce to 1
        raise ValueError("min_chars must be an integer")
    try:
        min_chars = int(raw_min)
    except (TypeError, ValueError) as exc:
        raise ValueError("min_chars must be an integer") from exc
    if min_chars < 1:
        raise ValueError("min_chars must be >= 1")

    rules_in = config.get("rules") or []
    if not isinstance(rules_in, list):
        raise ValueError("rules must be a list")
    rules: list[dict[str, str]] = []
    for raw in rules_in:
        if not isinstance(raw, dict):
            raise ValueError("each rule must be an object")
        rule = _normalize_rule(raw)
        if not rule["host"]:
            raise ValueError("each rule needs a non-empty host")
        if not _HOST_RE.match(rule["host"]):
            raise ValueError("host must be a bare domain, e.g. example.com (no scheme, path, or port)")
        if rule["proxy"] and rule["proxy"] not in PROXY_KEYS:
            raise ValueError(f"rule proxy must be one of {list(PROXY_KEYS)} (or empty for default)")
        if rule["proxy"] == "custom":
            _validate_custom_template(rule["custom_template"])
        rules.append(rule)
    return {"default_proxy": default_proxy, "min_chars": min_chars, "rules": rules}


def load(conn: sqlite3.Connection) -> dict[str, Any]:
    raw = settings_store.get(conn, _KEY)
    if not raw:
        return _defaults()
    try:
        return _validate(json.loads(raw))
    except (ValueError, TypeError, json.JSONDecodeError):
        # A corrupt stored blob must never break extraction.
        return _defaults()


def save(conn: sqlite3.Connection, config: dict[str, Any]) -> dict[str, Any]:
    validated = _validate(config)
    settings_store.set_(conn, _KEY, json.dumps(validated))
    return validated
