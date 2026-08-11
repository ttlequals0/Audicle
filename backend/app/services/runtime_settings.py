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
        # Authenticated feeds. Stored here so the renderer/guard read them via the
        # overlay; managed through /api/v1/feed-auth, not the generic PUT form.
        # FEED_AUTH_KEY is deliberately NOT in MASKED_KEYS -- the operator must
        # read it to build the subscribe URL.
        "FEED_AUTH_ENABLED",
        "FEED_AUTH_KEY",
        "RETENTION_DAYS",
        # Per-file upload size ceiling in MB; tunable live for image-heavy PDFs.
        "UPLOAD_MAX_MB",
        # OCR for scanned/image uploads (0.51.0).
        "OCR_ENABLED",
        "OCR_MAX_PAGES",
        "OCR_DPI",
        "OCR_MIN_CONFIDENCE",
        "OCR_LANGUAGE",
        # Watchdog policy: the base + per-chunk budget size the absolute ceiling,
        # the stall window is what actually kills a job. Tunable live so long-form
        # documents and slower hardware get proportional synthesis time.
        "JOB_TIMEOUT_SECONDS",
        "JOB_TIMEOUT_PER_CHUNK_SECONDS",
        "JOB_STALL_SECONDS",
        "JOB_TIMEOUT_CEILING_MULTIPLIER",
        # Fire-and-forget webhook on terminal job transitions; empty disables.
        "WEBHOOK_URL",
        # Arc XP static body extractor toggle.
        "EXTRACTION_ARC_ENABLED",
        # Address used to answer free registration walls; empty disables it.
        "REGISTRATION_EMAIL",
        # Primary extraction engine (direct | firecrawl) + the direct fetch timeout,
        # tunable live so an operator can switch engines without an env edit + restart.
        "EXTRACTION_ENGINE",
        "EXTRACTION_DIRECT_TIMEOUT_SECONDS",
        # Connections: the bundled service URLs are operator-tunable so they can
        # be pointed at an external Firecrawl/TTS without an env edit + restart.
        "FIRECRAWL_URL",
        "FIRECRAWL_API_KEY",
        "TTS_URL",
        # TTS model + narration language (0.51.0): model applied per job start,
        # language rides every /generate call.
        "TTS_MODEL",
        "TTS_LANGUAGE",
        # FlareSolverr endpoint for the flaresolverr paywall strategy; operator-
        # tunable so they can point at their own solver without an env edit.
        "FLARESOLVERR_URL",
        # Reader-proxy ("reader" strategy) endpoint + optional API key. Live-tunable so an
        # operator can paste a Jina key (to clear the keyless rate limit) or point at a
        # self-hosted reader without an env edit. The key is masked on read (MASKED_KEYS).
        "READER_PROXY_TEMPLATE",
        "READER_API_KEY",
        # Render sidecar endpoint (empty disables); tunable live so an operator can point
        # at their own sidecar without an env edit. Which hosts use render is a per-host
        # Site-override rule now, not a setting. RENDER_TIMEOUT_SECONDS stays env-only
        # (a structural per-request budget, like FLARESOLVERR_MAX_TIMEOUT_MS).
        "RENDER_URL",
        # Try a Wayback capture as a last resort on a hard block; tunable live.
        "ARCHIVE_FALLBACK_ENABLED",
        "TTS_CHUNK_TARGET_WORDS",
        "TTS_CHUNK_MAX_WORDS",
        "TTS_CHUNK_MAX_CHARS",
        "TTS_CHUNK_SILENCE_MS",
        # Chatterbox generation knobs, forwarded to the wrapper per /generate
        # call. The worker snapshots the overlay once per job, so a Settings
        # change applies to the next job (auditions pick it up immediately);
        # no restart either way.
        "CHATTERBOX_TEMPERATURE",
        "CHATTERBOX_REPETITION_PENALTY",
        "CHATTERBOX_TOP_P",
        "CHATTERBOX_TOP_K",
        "CHATTERBOX_SEED",
        "CHATTERBOX_MAX_CHARS",
        "CHIME_ENABLED",
        # Audio-QA thresholds: tunable live since they need empirical tuning
        # against real failures. The frame/hop sizes stay env-only (structural).
        "AUDIO_ANALYSIS_ENABLED",
        "AUDIO_ANALYSIS_MAX_REGEN",
        "AUDIO_ANALYSIS_MIN_RMS_CV",
        "AUDIO_ANALYSIS_MIN_CREST",
        "AUDIO_ANALYSIS_MAX_ZCR",
        "AUDIO_ANALYSIS_MAX_F0_SEMITONES",
        "AUDIO_ANALYSIS_MAX_SILENT_FRACTION",
        "AUDIO_ANALYSIS_WORDS_PER_SEC",
        "AUDIO_ANALYSIS_DURATION_OVERHEAD_SECS",
        "AUDIO_ANALYSIS_MAX_DURATION_RATIO",
        "AUDIO_ANALYSIS_MIN_DURATION_RATIO",
        # How a regeneration differs from the take that failed. Shared by the
        # audio-QA and whisper paths, same as the MAX_REGEN budget above.
        "AUDIO_ANALYSIS_REGEN_CHARS_FACTOR",
        "AUDIO_ANALYSIS_REGEN_MIN_CHARS",
        "AUDIO_ANALYSIS_REGEN_PENALTY_STEP",
        # Post-TTS ASR verification policy. Tunable live so an operator can turn
        # the gate on/off and adjust strictness without a restart. The wrapper's
        # WHISPER_ENABLED (which loads the model) stays env-only -- it is a
        # separate-container startup capability, not a per-job policy.
        "WHISPER_VERIFY_ENABLED",
        "WHISPER_DIVERGENCE_THRESHOLD",
        "WHISPER_MAX_DIVERGENT_RUN",
        "WHISPER_VERIFY_MIN_WORDS",
        "WHISPER_SHORT_CHUNK_DIVERGENCE",
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
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "LLM_TEMPERATURE",
        "LLM_MAX_TOKENS",
        "LLM_CLEANUP_WINDOW_CHARS",
        "LLM_TIMEOUT_SECONDS",
        "LLM_RETRY_COUNT",
        "LLM_PRONUNCIATION_CONCURRENCY",
    }
)

# Secret-bearing keys: their stored value is never returned by GET (masked to a
# sentinel) so the Settings UI can show "set" without leaking the credential.
MASKED_KEYS: frozenset[str] = frozenset(
    {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "FIRECRAWL_API_KEY",
        "READER_API_KEY",
    }
)

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


def set_if_absent(conn: sqlite3.Connection, key: str, value: Any) -> str:
    """Store ``value`` only if ``key`` has no row yet (atomic ``INSERT OR
    IGNORE``), then return the stored serialized value. The first writer wins a
    race, so a generated secret stays stable once minted even under concurrent
    enables -- unlike ``set_value``'s last-writer-wins UPSERT."""

    if key not in ALLOWED_KEYS:
        raise KeyError(f"{key} is not an operator-tunable setting")
    conn.execute(
        """
        INSERT OR IGNORE INTO runtime_settings (key, value, updated_at)
        VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        """,
        (key, _serialize(value)),
    )
    conn.commit()
    row = conn.execute("SELECT value FROM runtime_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else _serialize(value)


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
