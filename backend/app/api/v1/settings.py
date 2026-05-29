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

from app.api.deps import require_admin
from app.config import Settings, get_settings
from app.core import database
from app.services import runtime_settings

router = APIRouter(tags=["settings"])


class SettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowlist: list[str]
    values: dict[str, Any]


@router.get(
    "/settings",
    response_model=SettingsResponse,
    dependencies=[Depends(require_admin)],
)
async def get_settings_overrides(
    settings: Annotated[Settings, Depends(get_settings)],
) -> SettingsResponse:
    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        stored = runtime_settings.get_all(conn)
    finally:
        conn.close()
    coerced = {
        key: (
            runtime_settings.MASK_SENTINEL
            if key in runtime_settings.MASKED_KEYS
            else _coerce(key, value, settings)
        )
        for key, value in stored.items()
    }
    return SettingsResponse(
        allowlist=sorted(runtime_settings.ALLOWED_KEYS),
        values=coerced,
    )


@router.put(
    "/settings",
    response_model=SettingsResponse,
    dependencies=[Depends(require_admin)],
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
    finally:
        conn.close()
    coerced = {
        key: (
            runtime_settings.MASK_SENTINEL
            if key in runtime_settings.MASKED_KEYS
            else _coerce(key, value, settings)
        )
        for key, value in stored.items()
    }
    return SettingsResponse(
        allowlist=sorted(runtime_settings.ALLOWED_KEYS),
        values=coerced,
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
