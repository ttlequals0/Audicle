"""``POST /api/v1/webhooks/test`` -- send a sample payload to the configured
``WEBHOOK_URL`` so an operator can verify their receiver from the Settings UI or
API before a real episode runs. Reads the runtime-overlaid value so a freshly
saved URL is used without a restart.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app.config import Settings, get_settings
from app.services import runtime_settings, webhooks

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class WebhookTestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivered: bool
    status_code: int | None
    error: str | None


@router.post("/test", response_model=WebhookTestResult)
async def test_webhook(
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebhookTestResult:
    """Deliver the sample payload once and report the outcome. 409 when no
    ``WEBHOOK_URL`` is configured."""

    effective = runtime_settings.overlay(settings)
    if not effective.WEBHOOK_URL.strip():
        raise HTTPException(status_code=409, detail="set a webhook URL in Settings first")
    return WebhookTestResult(**await webhooks.send_test(effective))
