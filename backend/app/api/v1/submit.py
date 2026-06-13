"""POST /api/v1/submit -- enqueue an article URL for processing."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

from app.api.deps import get_conn
from app.services import jobs, ssrf, voices

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
            "When true and the URL already has an episode, re-run the pipeline "
            "and update that episode in place (same episode_id, new pub_date). "
            "Default is to return 409."
        ),
    )
    voice: str | None = Field(
        default=None,
        max_length=16,
        description="Reference voice: a slot number 1-5, 'last', or 'random' (default).",
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
        description="True if reprocess=true and a prior episode for this URL existed.",
    )


@router.post(
    "/submit",
    status_code=201,
    response_model=SubmitResponse,
    summary="Submit an article URL for processing",
)
async def submit(
    body: SubmitRequest,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> SubmitResponse:
    # SSRF guard before enqueue: reject a URL whose host resolves to a private/
    # loopback/reserved address so the fetcher can't be pointed at internal
    # services. The resolved IP is deliberately not echoed back. A resolution
    # failure (transient DNS, NXDOMAIN) is NOT a confirmed-internal target, so it
    # is enqueued and left for the worker's fetch to handle, as before.
    try:
        await ssrf.assert_url_public(body.url)
    except ssrf.BlockedHostError as exc:
        if exc.blocked:
            raise HTTPException(
                status_code=400,
                detail="The submitted URL resolves to a non-public address and was blocked.",
            ) from exc
    voice_id = voices.resolve(conn, body.voice)
    try:
        result = jobs.create_job(
            conn, body.url, reprocess=body.reprocess, voice_id=voice_id
        )
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
    return SubmitResponse(
        job_id=result.job.id,
        episode_id=result.job.episode_id,
        status=result.job.status,
        replaced_previous=result.replaced_previous,
    )
