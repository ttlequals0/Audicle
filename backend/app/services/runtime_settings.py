"""Operator-tunable settings that override env defaults at request time.

Only an allowlisted subset of ``Settings`` fields is exposed -- the rest are
infrastructure-level (DB path, secret keys) and would be footguns to flip
without a restart. The resolver coerces stored strings back to the field's
declared type via ``Settings.__class__.model_fields`` introspection.

``overlay(settings)`` returns a copy of ``Settings`` with the
``runtime_settings`` row values applied on top of the env defaults. This is
the resolution chain:

    code default -> env var (Pydantic) -> runtime_settings DB row
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.config import Settings
from app.core import database

# Operator-tunable subset. Any field not in this set returns 400 on PUT and
# is invisible on GET; cosmetic/runtime tuning lives here, secrets and
# infrastructure paths do not.
ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "FEED_TITLE",
        "FEED_DESCRIPTION",
        "FEED_AUTHOR",
        "FEED_EMAIL",
        "FEED_LANGUAGE",
        "FEED_CATEGORY",
        "FEED_EXPLICIT",
        "FEED_ARTWORK_URL",
        "RETENTION_DAYS",
        "TTS_CHUNK_TARGET_WORDS",
        "TTS_CHUNK_MAX_WORDS",
        "TTS_CHUNK_SILENCE_MS",
        "RSS_CACHE_MAX_AGE_SECONDS",
        "MIN_CLEANUP_CHARS",
        "MAX_PROMPT_LENGTH_BYTES",
        # LLM provider group (build-plan Settings UI). API keys are stored but
        # masked on read -- see MASKED_KEYS and api/v1/settings.py.
        "LLM_PROVIDER",
        "LLM_MODEL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "LLM_TEMPERATURE",
        "LLM_MAX_TOKENS",
        "LLM_TIMEOUT_SECONDS",
        "LLM_RETRY_COUNT",
    }
)

# Secret-bearing keys: their stored value is never returned by GET (masked to a
# sentinel) so the Settings UI can show "set" without leaking the credential.
MASKED_KEYS: frozenset[str] = frozenset({"OPENAI_API_KEY", "ANTHROPIC_API_KEY"})

# Sentinel returned by GET for a masked key that has a stored override, and
# recognized by PUT as "leave unchanged" so re-saving the form doesn't clobber
# the secret with the mask.
MASK_SENTINEL = "********"


def get_all(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM runtime_settings").fetchall()
    return {row["key"]: row["value"] for row in rows if row["key"] in ALLOWED_KEYS}


def set_value(conn: sqlite3.Connection, key: str, value: Any) -> None:
    if key not in ALLOWED_KEYS:
        raise KeyError(f"{key} is not an operator-tunable setting")
    serialized = _serialize(value)
    conn.execute(
        """
        INSERT INTO runtime_settings (key, value, updated_at)
        VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, serialized),
    )
    conn.commit()


def delete(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM runtime_settings WHERE key = ?", (key,))
    conn.commit()


def overlay(settings: Settings) -> Settings:
    """Return ``settings`` with the ``runtime_settings`` row values applied
    on top of the env defaults.

    Reads the DB once per call. Callers that hit a hot path should cache
    the result for the duration of the request -- ``api.deps.runtime_settings``
    does this via ``Depends``. ``Settings`` is a frozen-ish Pydantic model;
    we use ``model_copy(update=...)`` so the result is a new instance and
    the cached singleton from ``get_settings()`` is not mutated.
    """

    with database.connection(settings.DATA_DIR) as conn:
        stored = get_all(conn)
    if not stored:
        return settings
    coerced: dict[str, Any] = {}
    for key, raw_value in stored.items():
        field = settings.__class__.model_fields.get(key)
        if field is None:
            continue
        coerced[key] = _coerce_for_field(raw_value, field.annotation)
    return settings.model_copy(update=coerced)


def _coerce_for_field(value: str, annotation: Any) -> Any:
    """Best-effort coercion mirroring the api/v1/settings.py route logic."""

    if annotation is bool:
        try:
            return bool(json.loads(value))
        except (TypeError, ValueError):
            return value.lower() in {"true", "1", "yes"}
    if annotation is int:
        try:
            return int(json.loads(value))
        except (TypeError, ValueError):
            try:
                return int(value)
            except ValueError:
                return value
    if annotation is float:
        try:
            return float(json.loads(value))
        except (TypeError, ValueError):
            try:
                return float(value)
            except ValueError:
                return value
    return value


def _serialize(value: Any) -> str:
    if isinstance(value, bool | int | float):
        return json.dumps(value)
    if isinstance(value, str):
        return value
    return json.dumps(value)
