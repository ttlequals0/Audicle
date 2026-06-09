"""Pronunciation corrections API.

The ``lexicon`` table is the single source of truth. These endpoints read and
edit the operator's ``user`` rows in the object schema
``{input: {mode, spoken, ipa?, case_sensitive?}}``; the read-only seed/base rows
are inspectable (``/seed``, ``/lookup``) and exportable (``/export``) but not
editable here.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Annotated, Any
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.config import Settings, get_settings
from app.core import database
from app.services import corrections as corrections_service
from app.services import lexicon, pronounce_convert

router = APIRouter(tags=["corrections"])


@router.get("/corrections", summary="Read the user pronunciation corrections")
def read_corrections(settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, Any]:
    with database.connection(settings.DATA_DIR) as conn:
        return lexicon.get_user_entries(conn)


@router.put("/corrections", summary="Replace the user pronunciation corrections")
def write_corrections(
    body: Annotated[dict[str, Any], Body(...)],
    settings: Annotated[Settings, Depends(get_settings)],
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
    # spoken respelling the engine consumes (the ipa column is still stored but
    # unused now that Chatterbox is the only, text-only, engine).
    entries: dict[str, dict] = {}
    for key, value in body.items():
        if isinstance(value, str):
            spoken, ipa, mode, cs = value, None, None, None
        else:
            spoken, ipa = value.get("spoken"), value.get("ipa")
            mode, cs = value.get("mode"), value.get("case_sensitive")
        converted = pronounce_convert.convert_entry(
            key, spoken=spoken, ipa=ipa, case_sensitive=cs
        )
        entries[key] = {
            "mode": mode or converted.mode,
            "spoken": converted.spoken,
            "ipa": converted.ipa,
            "case_sensitive": converted.case_sensitive,
            "confidence": converted.confidence,
            "source": "user",
        }
    with database.connection(settings.DATA_DIR) as conn:
        lexicon.replace_user_entries(conn, entries)
        return lexicon.get_user_entries(conn)


@router.delete("/corrections", summary="Clear the user pronunciation corrections")
def reset_corrections(settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, Any]:
    with database.connection(settings.DATA_DIR) as conn:
        lexicon.replace_user_entries(conn, {})
    return {}


@router.get("/corrections/seed", summary="Read the built-in seed corrections (read-only)")
def read_seed_corrections(settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, Any]:
    with database.connection(settings.DATA_DIR) as conn:
        entries = [asdict(e) for e in lexicon.iter_entries(conn, "all") if e.origin == "seed"]
    return {"entries": entries, "count": len(entries)}


@router.get("/corrections/lookup", summary="Look up a term in the full lexicon")
def lookup_correction(
    settings: Annotated[Settings, Depends(get_settings)],
    q: Annotated[str, Query(min_length=1, max_length=100)],
) -> dict[str, Any]:
    with database.connection(settings.DATA_DIR) as conn:
        entry = lexicon.lookup(conn, q)
    return {"query": q, "entry": asdict(entry) if entry else None}


@router.get("/corrections/export", summary="Export corrections as JSON or PLS")
def export_corrections(
    settings: Annotated[Settings, Depends(get_settings)],
    format: Annotated[str, Query(pattern="^(json|pls)$")] = "json",
    scope: Annotated[str, Query(pattern="^(user|all)$")] = "user",
) -> StreamingResponse:
    def json_stream():
        yield "{\n"
        first = True
        with database.connection(settings.DATA_DIR) as conn:
            for e in lexicon.iter_entries(conn, scope):
                prefix = "" if first else ",\n"
                first = False
                obj = {"mode": e.mode, "spoken": e.spoken, "ipa": e.ipa,
                       "case_sensitive": e.case_sensitive}
                yield f"{prefix}  {json.dumps(e.input_text)}: {json.dumps(obj)}"
        yield "\n}\n"

    def pls_stream():
        yield (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<lexicon version="1.0" xmlns="http://www.w3.org/2005/01/pronunciation-lexicon" '
            'alphabet="ipa" xml:lang="en">\n'
        )
        with database.connection(settings.DATA_DIR) as conn:
            for e in lexicon.iter_entries(conn, scope):
                grapheme = xml_escape(e.input_text)
                if e.ipa:
                    body = f"<phoneme>{xml_escape(e.ipa)}</phoneme>"
                else:
                    body = f"<alias>{xml_escape(e.spoken)}</alias>"
                yield f"  <lexeme><grapheme>{grapheme}</grapheme>{body}</lexeme>\n"
        yield "</lexicon>\n"

    if format == "pls":
        return StreamingResponse(pls_stream(), media_type="application/pls+xml")
    return StreamingResponse(json_stream(), media_type="application/json")
