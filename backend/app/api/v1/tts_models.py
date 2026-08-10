"""TTS wrapper proxy: the model list for the Settings UI, so the UI never
talks to the wrapper directly."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app.config import Settings, get_settings
from app.services import tts

router = APIRouter(tags=["tts"])


class TtsModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    languages: list[str]


class TtsModelsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    active: str | None
    models: list[TtsModel]


@router.get("/tts/models", response_model=TtsModelsResponse)
async def list_tts_models(
    settings: Annotated[Settings, Depends(get_settings)],
) -> TtsModelsResponse:
    try:
        body = await tts.list_models(settings)
    except tts.TTSError as exc:
        raise HTTPException(status_code=502, detail=f"TTS wrapper unavailable: {exc}") from exc
    return TtsModelsResponse.model_validate(body)
