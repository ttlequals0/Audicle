"""Persist desired sidecar settings and apply them when a sidecar is reachable."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

import httpx
from fastapi import HTTPException

RENDER_KEYS = frozenset(
    {
        "RENDER_NAV_TIMEOUT_MS",
        "RENDER_CLICK_TIMEOUT_MS",
        "RENDER_GROW_WAIT_MS",
        "RENDER_ATTEMPTS",
        "RENDER_SETTLE_POLL_MS",
        "RENDER_SETTLE_QUIET_POLLS",
        "RENDER_SETTLE_MAX_MS",
        "RENDER_BUDGET_SECONDS",
        "LOG_LEVEL",
        "LOG_FORMAT",
    }
)
TTS_KEYS = frozenset(
    {
        "TTS_MEMORY_SOFT_LIMIT_MB",
        "TTS_MEMORY_HARD_LIMIT_MB",
        "TTS_IDLE_UNLOAD_SECONDS",
        "WHISPER_ENABLED",
        "WHISPER_MODEL",
        "WHISPER_DEVICE",
        "WHISPER_COMPUTE_TYPE",
        "TTS_REQUEST_TIMEOUT_SECONDS",
        "LOG_LEVEL",
        "LOG_FORMAT",
    }
)
_PREFIX = "sidecar."
_LOG_KEYS = frozenset({"LOG_LEVEL", "LOG_FORMAT"})
_BOUNDS: dict[str, tuple[int | float, int | float]] = {
    "RENDER_NAV_TIMEOUT_MS": (1000, 180_000),
    "RENDER_CLICK_TIMEOUT_MS": (100, 60_000),
    "RENDER_GROW_WAIT_MS": (0, 30_000),
    "RENDER_ATTEMPTS": (1, 10),
    "RENDER_SETTLE_POLL_MS": (50, 5000),
    "RENDER_SETTLE_QUIET_POLLS": (1, 20),
    "RENDER_SETTLE_MAX_MS": (100, 60_000),
    "RENDER_BUDGET_SECONDS": (1, 300),
    "TTS_MEMORY_SOFT_LIMIT_MB": (0, 100_000),
    "TTS_MEMORY_HARD_LIMIT_MB": (0, 100_000),
    "TTS_IDLE_UNLOAD_SECONDS": (0, 86_400),
    "TTS_REQUEST_TIMEOUT_SECONDS": (1, 3600),
}
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_INTEGER_KEYS = frozenset(
    key
    for key in _BOUNDS
    if key not in {"TTS_REQUEST_TIMEOUT_SECONDS", "RENDER_BUDGET_SECONDS"}
)


def get_saved(conn: sqlite3.Connection, sidecar: str) -> dict[str, Any]:
    allowed = _keys(sidecar)
    rows = conn.execute("SELECT key, value FROM runtime_settings WHERE key LIKE ?", (_PREFIX + sidecar + ".%",))
    result = {}
    for row in rows:
        key = row["key"].removeprefix(_PREFIX + sidecar + ".")
        if key in allowed:
            result[key] = json.loads(row["value"])
    return result


def save(conn: sqlite3.Connection, sidecar: str, updates: dict[str, Any | None]) -> dict[str, Any]:
    return save_many(conn, {sidecar: updates})[sidecar]


def save_many(
    conn: sqlite3.Connection, updates_by_sidecar: dict[str, dict[str, Any | None]]
) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any | None]] = {}
    for sidecar, updates in updates_by_sidecar.items():
        allowed = _keys(sidecar)
        unknown = set(updates) - allowed
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"unknown {sidecar} settings: {sorted(unknown)}"
            )
        normalized[sidecar] = dict(updates)
        for key, value in updates.items():
            if value is not None:
                _validate(key, value)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for sidecar, updates in normalized.items():
            for key, value in updates.items():
                db_key = f"{_PREFIX}{sidecar}.{key}"
                if value is None:
                    conn.execute("DELETE FROM runtime_settings WHERE key = ?", (db_key,))
                else:
                    conn.execute(
                        """INSERT INTO runtime_settings (key, value, updated_at)
                        VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                        (db_key, json.dumps(value)),
                    )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {sidecar: get_saved(conn, sidecar) for sidecar in normalized}


def _keys(sidecar: str) -> frozenset[str]:
    if sidecar == "render":
        return RENDER_KEYS
    if sidecar == "tts_wrapper":
        return TTS_KEYS
    raise ValueError(f"unknown sidecar: {sidecar}")


def _validate(key: str, value: Any) -> None:
    if key in _BOUNDS:
        low, high = _BOUNDS[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or (key in _INTEGER_KEYS and not isinstance(value, int))
            or not low <= value <= high
        ):
            raise HTTPException(status_code=400, detail=f"{key} must be between {low} and {high}")
    elif key in {"WHISPER_MODEL", "WHISPER_COMPUTE_TYPE"}:
        if not isinstance(value, str) or not _MODEL_RE.fullmatch(value):
            raise HTTPException(status_code=400, detail=f"{key} must be a valid model identifier")
    elif key == "WHISPER_DEVICE" and (
        not isinstance(value, str) or value not in {"cuda", "cpu"}
    ):
        raise HTTPException(status_code=400, detail="WHISPER_DEVICE must be cuda or cpu")
    elif key == "WHISPER_ENABLED" and not isinstance(value, bool):
        raise HTTPException(status_code=400, detail="WHISPER_ENABLED must be boolean")
    elif key == "LOG_LEVEL" and (
        not isinstance(value, str)
        or value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    ):
        raise HTTPException(status_code=400, detail="LOG_LEVEL is invalid")
    elif key == "LOG_FORMAT" and (
        not isinstance(value, str) or value not in {"json", "text"}
    ):
        raise HTTPException(status_code=400, detail="LOG_FORMAT must be json or text")


def validate_memory_limits(
    desired: dict[str, Any], defaults: dict[str, Any] | None = None
) -> None:
    baseline = defaults or {}
    soft = desired.get("TTS_MEMORY_SOFT_LIMIT_MB", baseline.get("TTS_MEMORY_SOFT_LIMIT_MB"))
    hard = desired.get("TTS_MEMORY_HARD_LIMIT_MB", baseline.get("TTS_MEMORY_HARD_LIMIT_MB"))
    if soft is not None and hard is not None and soft and hard and soft > hard:
        raise HTTPException(
            status_code=400,
            detail="TTS_MEMORY_SOFT_LIMIT_MB must not exceed TTS_MEMORY_HARD_LIMIT_MB",
        )


async def fetch_config(base_url: str, sidecar: str) -> dict[str, Any] | None:
    if not base_url:
        return None
    base_url = base_url.rstrip("/").removesuffix("/render").removesuffix("/generate")
    endpoint = base_url + "/runtime-config"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5, connect=2)) as client:
            response = await client.get(endpoint)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("defaults"), dict):
            return payload
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        return None
    return None


async def apply_config(base_url: str, sidecar: str, values: dict[str, Any]) -> dict[str, Any] | None:
    if sidecar == "render":
        values = {key: value for key, value in values.items() if key in _LOG_KEYS}
    elif sidecar != "tts_wrapper":
        return None
    if not base_url:
        return None
    base_url = base_url.rstrip("/").removesuffix("/render").removesuffix("/generate")
    endpoint = base_url + "/runtime-config"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=2)) as client:
            response = await client.put(endpoint, json={"values": values})
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else None
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        return None


async def ensure_config(
    base_url: str, sidecar: str, desired: dict[str, Any]
) -> dict[str, Any] | None:
    current = await fetch_config(base_url, sidecar)
    if current is None:
        return None
    desired = (
        {key: value for key, value in desired.items() if key in _LOG_KEYS}
        if sidecar == "render"
        else desired
    )
    defaults = current.get("defaults", {})
    effective = current.get("effective", defaults)
    expected = {**defaults, **desired}
    if all(effective.get(key) == value for key, value in expected.items()):
        return current
    return await apply_config(base_url, sidecar, desired)
