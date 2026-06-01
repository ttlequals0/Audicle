"""``GET/PUT /api/v1/settings`` -- operator-tunable runtime overrides.

Only the allowlisted subset (``runtime_settings.ALLOWED_KEYS``) is exposed.
Unknown keys on PUT return 400 with a list of accepted keys. Values are
type-coerced against the ``Settings`` field annotations so a stored string
``"450"`` for ``RETENTION_DAYS`` round-trips as an int.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app.config import Settings, get_settings
from app.core import database
from app.services import runtime_settings, settings_store, slug

router = APIRouter(tags=["settings"])


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
) -> SettingsResponse:
    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        stored = runtime_settings.get_all(conn)
    finally:
        conn.close()
    return _masked_response(stored, settings)


@router.put(
    "/settings",
    response_model=SettingsResponse,
)
async def put_settings_overrides(
    payload: Annotated[dict[str, Any], Body()],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SettingsResponse:
    unknown = [k for k in payload if k not in runtime_settings.ALLOWED_KEYS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown setting keys: {unknown}; allowed: {sorted(runtime_settings.ALLOWED_KEYS)}"
            ),
        )
    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        # The current feed slug, before applying, so a FEED_TITLE rename can be
        # detected below. Derived live from the effective title (no stored copy
        # to drift from the live FEED_TITLE the feed is actually served at).
        old_slug = slug.feed_slug(_effective_title(runtime_settings.get_all(conn), settings))

        for key, value in payload.items():
            if key in runtime_settings.MASKED_KEYS:
                # Re-saving the form sends back the mask sentinel for an
                # unchanged secret -- skip it so the stored value survives.
                # An explicit empty string clears the override (revert to env).
                if value == runtime_settings.MASK_SENTINEL:
                    continue
                if value == "":
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
    finally:
        conn.close()
    return _masked_response(stored, settings)


def _masked_response(stored: dict[str, str], settings: Settings) -> SettingsResponse:
    """Build the GET/PUT response, masking secret-bearing keys so their stored
    value is never echoed to the client."""

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
        feed_url=slug.feed_url(settings.BASE_URL, _effective_title(stored, settings)),
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
