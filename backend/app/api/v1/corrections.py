"""Pronunciation corrections API.

The ``lexicon`` table is the single source of truth. These endpoints read and
edit the operator's ``user`` rows in the object schema
``{input: {mode, spoken, case_sensitive?}}``; the read-only seed/base rows
are inspectable (``/seed``, ``/lookup``) and exportable (``/export``) but not
editable here.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api.deps import get_conn
from app.config import Settings, get_settings
from app.services import corrections as corrections_service
from app.services import lexicon, pronounce_convert

router = APIRouter(tags=["corrections"])


@router.get("/corrections", summary="Read the user pronunciation corrections")
def read_corrections(conn: Annotated[sqlite3.Connection, Depends(get_conn)]) -> dict[str, Any]:
    return lexicon.get_user_entries(conn)


@router.put("/corrections", summary="Replace the user pronunciation corrections")
def write_corrections(
    body: Annotated[dict[str, Any], Body(...)],
    settings: Annotated[Settings, Depends(get_settings)],
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> dict[str, Any]:
    result = corrections_service.validate_lexicon(body, max_entries=settings.MAX_CORRECTIONS_ENTRIES)
    if not result.ok:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Corrections validation failed",
                "details": {
                    "failures": [{"key": f.key, "reason": f.reason} for f in result.failures]
                },
            },
        )
    # Normalize each entry through the shared converter so user rows get the
    # spoken respelling the (text-only Chatterbox) engine consumes.
    entries: dict[str, dict] = {}
    for key, value in body.items():
        if isinstance(value, str):
            spoken, mode, cs = value, None, None
        else:
            spoken = value.get("spoken")
            mode, cs = value.get("mode"), value.get("case_sensitive")
        converted = pronounce_convert.convert_entry(key, spoken=spoken, case_sensitive=cs)
        entries[key] = {
            "mode": mode or converted.mode,
            "spoken": converted.spoken,
            "case_sensitive": converted.case_sensitive,
            "confidence": converted.confidence,
            "source": "user",
        }
    lexicon.replace_user_entries(conn, entries)
    return lexicon.get_user_entries(conn)


@router.delete("/corrections", summary="Clear the user pronunciation corrections")
def reset_corrections(conn: Annotated[sqlite3.Connection, Depends(get_conn)]) -> dict[str, Any]:
    lexicon.replace_user_entries(conn, {})
    return {}


@router.get("/corrections/seed", summary="Read the built-in seed corrections (read-only)")
def read_seed_corrections(conn: Annotated[sqlite3.Connection, Depends(get_conn)]) -> dict[str, Any]:
    entries = [asdict(e) for e in lexicon.iter_entries(conn, "all") if e.origin == "seed"]
    return {"entries": entries, "count": len(entries)}


@router.get("/corrections/lookup", summary="Look up a term in the full lexicon")
def lookup_correction(
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    q: Annotated[str, Query(min_length=1, max_length=100)],
) -> dict[str, Any]:
    entry = lexicon.lookup(conn, q)
    return {"query": q, "entry": asdict(entry) if entry else None}


@router.get("/corrections/export", summary="Export corrections as JSON")
def export_corrections(
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    scope: Annotated[str, Query(pattern="^(user|all)$")] = "user",
) -> StreamingResponse:
    def json_stream():
        yield "{\n"
        first = True
        for e in lexicon.iter_entries(conn, scope):
            prefix = "" if first else ",\n"
            first = False
            obj = {"mode": e.mode, "spoken": e.spoken, "case_sensitive": e.case_sensitive}
            yield f"{prefix}  {json.dumps(e.input_text)}: {json.dumps(obj)}"
        yield "\n}\n"

    return StreamingResponse(json_stream(), media_type="application/json")
