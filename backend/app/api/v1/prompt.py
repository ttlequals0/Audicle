"""GET + PUT + DELETE /api/v1/prompt -- read/replace/reset a pipeline prompt.

``?kind=`` selects which one: ``cleanup`` (default, so existing callers are
unaffected), ``summary``, or ``pronunciation``. All three are stored and
defaulted the same way; before 0.50.1 only cleanup was reachable, leaving the
other two editable in code but not by an operator.

DB-backed: the packaged default ships in the image; an operator edit is stored
in the settings table and wins until reset. Nothing is written to disk.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.deps import get_conn
from app.config import Settings, get_settings
from app.services import prompt as prompt_service

router = APIRouter(tags=["prompt"])

# Defaulting to cleanup keeps every pre-0.50.1 caller (the Settings UI included)
# working unchanged; anything outside the Literal fails validation (400 via
# api/errors.py) rather than silently falling back to a different prompt.
KindQuery = Annotated[
    prompt_service.PromptKind,
    Query(description="Which pipeline prompt to act on."),
]


class PromptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(
        min_length=1,
        description="Full cleanup prompt as a single string. Must contain at least one non-whitespace character.",
    )

    @field_validator("prompt")
    @classmethod
    def _validate_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must contain at least one non-whitespace character")
        return value


class PromptResponse(BaseModel):
    prompt: str
    is_default: bool


@router.get("/prompt", response_model=PromptResponse, summary="Read a pipeline prompt")
def read_prompt(
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    kind: KindQuery = "cleanup",
) -> PromptResponse:
    prompt, default = prompt_service.load_with_flag(conn, kind)
    return PromptResponse(prompt=prompt, is_default=default)


@router.put("/prompt", response_model=PromptResponse, summary="Replace a pipeline prompt")
def write_prompt(
    body: PromptBody,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
    kind: KindQuery = "cleanup",
) -> PromptResponse:
    try:
        prompt_service.save_override(
            conn, kind, body.prompt, max_bytes=settings.MAX_PROMPT_LENGTH_BYTES
        )
    except prompt_service.PromptTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "Prompt too large",
                "details": {
                    "max_bytes": settings.MAX_PROMPT_LENGTH_BYTES,
                    "actual_bytes": len(body.prompt.encode("utf-8")),
                },
            },
        ) from exc
    return PromptResponse(prompt=body.prompt, is_default=False)


@router.delete("/prompt", response_model=PromptResponse, summary="Reset a pipeline prompt to default")
def reset_prompt(
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    kind: KindQuery = "cleanup",
) -> PromptResponse:
    prompt_service.reset(conn, kind)
    return PromptResponse(prompt=prompt_service.load_effective(conn, kind), is_default=True)
