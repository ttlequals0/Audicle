"""GET + PUT /api/v1/corrections -- read/replace the pronunciation dictionary."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException

from app.config import Settings, get_settings
from app.core import database
from app.services import corrections as corrections_service
from app.services import seed_corrections

router = APIRouter(tags=["corrections"])


@router.get(
    "/corrections",
    summary="Read the pronunciation dictionary",
)
def read_corrections(settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, str]:
    with database.connection(settings.DATA_DIR) as conn:
        try:
            return corrections_service.load_user_dict(conn)
        except ValueError as exc:
            # Stored value is somehow not a JSON object; surface clearly.
            raise HTTPException(
                status_code=500, detail=f"stored corrections invalid: {exc}"
            ) from exc


@router.put(
    "/corrections",
    summary="Replace the pronunciation dictionary",
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
    with database.connection(settings.DATA_DIR) as conn:
        corrections_service.save_user_dict(conn, body)
    return body


@router.delete(
    "/corrections",
    summary="Reset the pronunciation dictionary to empty",
)
def reset_corrections(settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, str]:
    with database.connection(settings.DATA_DIR) as conn:
        corrections_service.save_user_dict(conn, {})
    return {}


@router.get(
    "/corrections/seed",
    summary="Read the built-in seed pronunciation corrections",
)
def read_seed_corrections() -> dict[str, Any]:
    """Return the bundled baseline corrections (read-only).

    These ship with Audicle, are not editable, and are applied beneath the
    user's own corrections (which win on key collision). ``applicable`` marks
    rows that are actually applied in the pipeline -- annotated homographs and
    spelled-out acronyms are listed for reference but not applied.
    """

    entries = seed_corrections.load_seed(seed_corrections.seed_path())
    return {
        "entries": [asdict(e) for e in entries],
        "count": len(entries),
        "applicable_count": sum(e.applicable for e in entries),
    }
