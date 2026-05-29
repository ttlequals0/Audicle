"""POST /api/v1/submit -- enqueue an article URL for processing."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

from app.api.deps import require_admin
from app.config import Settings, get_settings
from app.core import database
from app.services import jobs

router = APIRouter(tags=["jobs"])


class SubmitRequest(BaseModel):
    # extra="forbid" so client-side typos like {"reprcess": true} surface as
    # 400 Validation failed instead of being silently dropped.
    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        # 2048 covers every legitimate article URL we've seen in practice
        # and caps a pathological-URL DoS at a reasonable size.
        max_length=2048,
        min_length=1,
        description="HTTP/HTTPS URL of the article to ingest.",
    )
    reprocess: bool = Field(
        default=False,
        description=(
            "When true and the URL already has an episode/job, wipe the prior "
            "state and start fresh. Default is to return 409."
        ),
    )

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        # Validate with AnyHttpUrl semantics but return the exact string the
        # user submitted so the persisted url/episode_id are stable against the
        # raw input (pydantic's str(AnyHttpUrl) normalizes host casing and
        # appends a trailing slash on bare hosts).
        AnyHttpUrl(value)
        return value


class SubmitResponse(BaseModel):
    job_id: str
    episode_id: str
    status: str
    replaced_previous: bool = Field(
        default=False,
        description="True if reprocess=true and a prior episode/job was deleted.",
    )


@router.post(
    "/submit",
    status_code=201,
    response_model=SubmitResponse,
    summary="Submit an article URL for processing",
    dependencies=[Depends(require_admin)],
)
def submit(
    body: SubmitRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> SubmitResponse:
    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        try:
            result = jobs.create_job(conn, body.url, reprocess=body.reprocess)
        except jobs.DuplicateSubmissionError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "Episode already exists",
                    "details": {
                        "episode_id": exc.episode_id,
                        "reason": exc.reason,
                        "url": body.url,
                    },
                },
            ) from exc
    finally:
        conn.close()
    return SubmitResponse(
        job_id=result.job.id,
        episode_id=result.job.episode_id,
        status=result.job.status,
        replaced_previous=result.replaced_previous,
    )
