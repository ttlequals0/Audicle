"""``GET/PUT /api/v1/settings`` -- operator-tunable runtime overrides.

Only the allowlisted subset (``runtime_settings.ALLOWED_KEYS``) is exposed.
Unknown keys on PUT return 400 with a list of accepted keys. Values are
type-coerced against the ``Settings`` field annotations so a stored string
``"450"`` for ``RETENTION_DAYS`` round-trips as an int.
"""

from __future__ import annotations

import json
import operator
import re
import sqlite3
from typing import Annotated, Any, Literal, get_args, get_origin

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app.api.deps import get_conn
from app.config import RUNTIME_SETTING_BOUNDS, Settings, get_settings
from app.services import feed, feed_auth, runtime_settings, settings_store, slug
from app.services.ocr import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES

router = APIRouter(tags=["settings"])

# In ALLOWED_KEYS (so the overlay reads them) but write-only through their own
# endpoint; the generic form must not set them. See put_settings_overrides.
_ENDPOINT_MANAGED_KEYS = frozenset({"FEED_AUTH_ENABLED", "FEED_AUTH_KEY"})


class SettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowlist: list[str]
    # Stored operator overrides only (empty until something is saved).
    values: dict[str, Any]
    # Effective env/code default for each allowlisted key, so the UI can show
    # editable defaults instead of blank fields. Secret keys are masked.
    defaults: dict[str, Any]
    # The public feed URL, slug-derived from the effective FEED_TITLE, so the UI
    # shows the real subscribe URL without reimplementing slugify client-side.
    feed_url: str


@router.get(
    "/settings",
    response_model=SettingsResponse,
)
async def get_settings_overrides(
    settings: Annotated[Settings, Depends(get_settings)],
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> SettingsResponse:
    stored = runtime_settings.get_all(conn)
    return _masked_response(stored, settings, _effective_feed_key(conn, settings))


class OcrLanguagesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    languages: list[str]
    default: str


@router.get(
    "/settings/ocr/languages",
    response_model=OcrLanguagesResponse,
)
async def get_ocr_languages() -> OcrLanguagesResponse:
    """Languages the shipped OCR model packs cover; drives the Settings dropdown."""

    return OcrLanguagesResponse(languages=list(SUPPORTED_LANGUAGES), default=DEFAULT_LANGUAGE)


@router.put(
    "/settings",
    response_model=SettingsResponse,
)
async def put_settings_overrides(
    payload: Annotated[dict[str, Any], Body()],
    settings: Annotated[Settings, Depends(get_settings)],
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> SettingsResponse:
    unknown = [k for k in payload if k not in runtime_settings.ALLOWED_KEYS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown setting keys: {unknown}; allowed: {sorted(runtime_settings.ALLOWED_KEYS)}"
            ),
        )
    # FEED_AUTH_ENABLED/FEED_AUTH_KEY are in ALLOWED_KEYS so the overlay reads
    # them, but they must only be written through /api/v1/feed-auth: that path
    # lazily mints a valid 64-hex key on enable. Writing them here would let a
    # caller enable auth with no/invalid key and fail-closed 401 the whole feed.
    managed = sorted(_ENDPOINT_MANAGED_KEYS.intersection(payload))
    if managed:
        raise HTTPException(
            status_code=400,
            detail=f"{managed} are managed via /api/v1/feed-auth, not the settings form",
        )
    # The current feed slug, before applying, so a FEED_TITLE rename can be
    # detected below. Derived live from the effective title (no stored copy
    # to drift from the live FEED_TITLE the feed is actually served at).
    old_slug = slug.feed_slug(_effective_title(runtime_settings.get_all(conn), settings))

    # Validate every value before applying any, so a bad value in a multi-key save
    # can't partially apply -- and an invalid enum (e.g. EXTRACTION_ENGINE) is
    # rejected here instead of being stored and crashing/mis-routing at overlay time.
    for key, value in payload.items():
        # Re-saving the form sends the mask sentinel back for an unchanged secret,
        # and "" for any key means clear -- neither is a value to validate.
        if key in runtime_settings.MASKED_KEYS and value == runtime_settings.MASK_SENTINEL:
            continue
        if value == "":
            continue
        _validate_value(key, value, settings)

    for key, value in payload.items():
        if key in runtime_settings.MASKED_KEYS and value == runtime_settings.MASK_SENTINEL:
            continue  # unchanged secret: keep what is stored
        if value == "":
            # Clearing a field drops the override so the key reverts to its env
            # value. Storing "" instead would pin an empty FEED_TITLE/language.
            runtime_settings.delete(conn, key)
            continue
        runtime_settings.set_value(conn, key, value)

    stored = runtime_settings.get_all(conn)
    # Rename = new feed: if FEED_TITLE's slug changed, rotate the channel
    # podcast:guid and bump the epoch (which re-salts every episode <guid>),
    # so podcast apps treat it as a fresh feed and re-download. new_slug comes
    # from the stored value (always a coerced string), so a non-string
    # FEED_TITLE in the payload can't reach slugify.
    if "FEED_TITLE" in payload and slug.feed_slug(_effective_title(stored, settings)) != old_slug:
        settings_store.rotate_feed_guids(conn, settings.BASE_URL)
    return _masked_response(stored, settings, _effective_feed_key(conn, settings))


def _effective_feed_key(conn: sqlite3.Connection, settings: Settings) -> str | None:
    """The feed key to advertise in ``feed_url``: the active key when auth is on,
    else None so the URL stays keyless."""

    enabled, key = feed_auth.effective_auth(conn, settings)
    return key if enabled else None


def _masked_response(
    stored: dict[str, str], settings: Settings, feed_key: str | None = None
) -> SettingsResponse:
    """Build the GET/PUT response, masking secret-bearing keys so their stored
    value is never echoed to the client. ``feed_url`` carries the auth key when
    authenticated feeds are on, so the Feed page's copy button matches."""

    values = {
        key: (
            runtime_settings.MASK_SENTINEL
            if key in runtime_settings.MASKED_KEYS
            else _coerce(key, value, settings)
        )
        for key, value in stored.items()
    }
    return SettingsResponse(
        allowlist=sorted(runtime_settings.ALLOWED_KEYS),
        values=values,
        defaults=_defaults_map(settings),
        feed_url=feed.feed_url_with_key(
            settings.BASE_URL, _effective_title(stored, settings), feed_key
        ),
    )


def _effective_title(stored: dict[str, str], settings: Settings) -> str | None:
    """The FEED_TITLE in effect: the stored override, else the env/code value.
    ``slug.feed_slug``/``feed_url`` apply the default when this is empty."""

    return stored.get("FEED_TITLE") or settings.FEED_TITLE


def _defaults_map(settings: Settings) -> dict[str, Any]:
    """The effective env/code value for each allowlisted key (what the app uses
    when there is no override). Secret keys are masked so an env-set credential
    is never echoed -- the sentinel just signals 'a key is configured'."""

    defaults: dict[str, Any] = {}
    for key in runtime_settings.ALLOWED_KEYS:
        value = getattr(settings, key, None)
        if key in runtime_settings.MASKED_KEYS:
            defaults[key] = runtime_settings.MASK_SENTINEL if value else ""
        else:
            defaults[key] = value
    return defaults


# Shape checks for free-text keys whose value is handed to something else later
# (the TTS wrapper, a publisher's signup form), so a hand-crafted PUT cannot store
# one that only fails far from here. The wrapper stays the authority on model names.
_STRING_SHAPES: dict[str, re.Pattern[str]] = {
    "TTS_MODEL": re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$"),
    "TTS_LANGUAGE": re.compile(r"^[a-z]{2,3}$"),
    # A typo here would be typed into publishers' signup forms, so check the shape.
    "REGISTRATION_EMAIL": re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
}


def _validate_value(key: str, value: Any, settings: Settings) -> None:
    """Reject a value that doesn't fit its ``Settings`` field, with a 400.

    Enum (``Literal``) fields must be one of their allowed members; int/float fields
    must be coercible and inside any ``RUNTIME_SETTING_BOUNDS`` range. Unknown
    fields and string/bool fields pass through (bool coercion is intentionally
    lenient). Runs before anything is stored, so a bad value never reaches the DB
    to crash or mis-route at overlay time -- or, for a bounded knob like
    CHATTERBOX_TEMPERATURE, to fail far away on every TTS call.
    """

    shape = _STRING_SHAPES.get(key)
    if shape is not None:
        # "" never reaches here: the caller treats it as a clear.
        if not shape.fullmatch(str(value)):
            raise HTTPException(
                status_code=400,
                detail=f"{key} must match {shape.pattern} (or be empty), got {value!r}",
            )
        return
    field = settings.__class__.model_fields.get(key)
    if field is None:
        return
    annotation = field.annotation
    if get_origin(annotation) is Literal:
        allowed = [str(arg) for arg in get_args(annotation)]
        if str(value) not in allowed:
            raise HTTPException(
                status_code=400, detail=f"{key} must be one of {allowed}, got {value!r}"
            )
        return
    if annotation in (int, float):
        coerced = _coerce(key, value if isinstance(value, str) else json.dumps(value), settings)
        if not isinstance(coerced, annotation):
            raise HTTPException(status_code=400, detail=f"{key} must be a {annotation.__name__}")
        _enforce_bounds(key, coerced)


# Bound name -> (comparison, symbol for the error message).
_BOUND_OPS: dict[str, tuple[Any, str]] = {
    "gt": (operator.gt, ">"),
    "ge": (operator.ge, ">="),
    "lt": (operator.lt, "<"),
    "le": (operator.le, "<="),
}


def _enforce_bounds(key: str, value: float) -> None:
    """Enforce the ``RUNTIME_SETTING_BOUNDS`` range declared for ``key``, if any."""

    for name, bound in RUNTIME_SETTING_BOUNDS.get(key, {}).items():
        compare, symbol = _BOUND_OPS[name]
        if not compare(value, bound):
            raise HTTPException(
                status_code=400,
                detail=f"{key} must be {symbol} {bound}, got {value}",
            )


def _coerce(key: str, value: str, settings: Settings) -> Any:
    """Coerce the stored string back to the declared field type.

    Falls back to the raw string if the field isn't found (forward-compat
    so a future config rename doesn't 500 an existing GET).
    """

    field = settings.__class__.model_fields.get(key)
    if field is None:
        return value
    annotation = field.annotation
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
