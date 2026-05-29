"""GET + PUT /api/v1/corrections -- read/replace the pronunciation dictionary."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException

from app.api.deps import require_admin
from app.config import Settings, get_settings
from app.services import corrections as corrections_service

router = APIRouter(tags=["corrections"])


def _corrections_path() -> Path:
    return Path(__file__).parent.parent.parent / "corrections" / "pronunciation.json"


@router.get(
    "/corrections",
    summary="Read the pronunciation dictionary",
    dependencies=[Depends(require_admin)],
)
def read_corrections() -> dict[str, str]:
    try:
        return corrections_service.load(_corrections_path())
    except ValueError as exc:
        # Malformed file on disk; surface clearly so the operator notices.
        raise HTTPException(status_code=500, detail=f"corrections file invalid: {exc}") from exc


@router.put(
    "/corrections",
    summary="Replace the pronunciation dictionary",
    dependencies=[Depends(require_admin)],
)
def write_corrections(
    # ``dict[str, Any]`` (not ``dict[str, str]``) so non-string values reach
    # corrections.validate and surface as the typed per-key failure envelope
    # instead of being short-circuited by pydantic's generic "Validation failed".
    body: Annotated[dict[str, Any], Body(...)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    result = corrections_service.validate(body, max_entries=settings.MAX_CORRECTIONS_ENTRIES)
    if not result.ok:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Corrections validation failed",
                "details": {
                    "failures": [{"key": f.key, "reason": f.reason} for f in result.failures],
                },
            },
        )
    corrections_service.save(_corrections_path(), body)
    return body
