"""GET + PUT /api/v1/prompt -- read/replace the cleanup prompt."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.deps import require_admin
from app.config import Settings, get_settings
from app.services import prompt as prompt_service

router = APIRouter(tags=["prompt"])


def _prompt_path() -> Path:
    return Path(__file__).parent.parent.parent / "prompts" / "script.txt"


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


@router.get("/prompt", response_model=PromptBody, summary="Read the cleanup prompt")
def read_prompt() -> PromptBody:
    path = _prompt_path()
    try:
        content = prompt_service.load(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Prompt file not found") from exc
    return PromptBody(prompt=content)


@router.put(
    "/prompt",
    response_model=PromptBody,
    summary="Replace the cleanup prompt",
    dependencies=[Depends(require_admin)],
)
def write_prompt(
    body: PromptBody,
    settings: Annotated[Settings, Depends(get_settings)],
) -> PromptBody:
    path = _prompt_path()
    try:
        prompt_service.save(path, body.prompt, max_bytes=settings.MAX_PROMPT_LENGTH_BYTES)
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
    return body
