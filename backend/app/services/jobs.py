"""Job table helpers.

Every mutation here bumps ``updated_at`` explicitly (no triggers). Callers
pass an open sqlite3 connection so the surrounding code controls transaction
boundaries.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("app.services.jobs")

JobStatus = str  # queued | processing | done | failed
JobStage = str  # extract | cleanup | corrections | chunk | tts | audio | artwork | transcript | finalize | done


@dataclass(frozen=True)
class Job:
    id: str
    url: str
    episode_id: str
    status: JobStatus
    stage: JobStage | None
    error: str | None
    created_at: str
    updated_at: str


def compute_episode_id(url: str) -> str:
    """MD5(url) truncated to 12 hex chars. Deterministic per URL."""

    # md5 here is a content identity hash, not a security primitive (the URL
    # is operator-supplied via /submit, not attacker-influenced). Marking
    # ``usedforsecurity=False`` silences CodeQL's ``py/weak-cryptographic-
    # algorithm`` finding which otherwise flags every md5 call regardless of
    # purpose. Identity-shortening to 12 hex chars is a deliberate trade-off
    # for a stable, short, lowercase-only episode id.
    return hashlib.md5(url.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        url=row["url"],
        episode_id=row["episode_id"],
        status=row["status"],
        stage=row["stage"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_job(conn: sqlite3.Connection, job_id: str) -> Job | None:
    row = conn.execute(
        "SELECT id, url, episode_id, status, stage, error, created_at, updated_at "
        "FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    return _row_to_job(row) if row else None


def get_job_by_episode_id(
    conn: sqlite3.Connection, episode_id: str, *, statuses: tuple[str, ...] | None = None
) -> Job | None:
    """Return the most recent job for ``episode_id``, optionally filtered by status."""

    base = (
        "SELECT id, url, episode_id, status, stage, error, created_at, updated_at "
        "FROM jobs WHERE episode_id = ?"
    )
    params: tuple = (episode_id,)
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        base += f" AND status IN ({placeholders})"
        params += statuses
    base += " ORDER BY created_at DESC LIMIT 1"
    row = conn.execute(base, params).fetchone()
    return _row_to_job(row) if row else None


def episode_exists(conn: sqlite3.Connection, episode_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    return row is not None


@dataclass(frozen=True)
class CreateJobResult:
    job: Job
    """The newly created job row."""
    replaced_previous: bool
    """True if ``reprocess=True`` and a prior episode for this URL existed (it is
    updated in place by the finalize stage, not deleted)."""


class DuplicateSubmissionError(Exception):
    """Raised when a non-reprocess submission collides with an existing episode
    or with an in-flight job. The submit endpoint converts this to a 409."""

    def __init__(self, episode_id: str, reason: str) -> None:
        super().__init__(f"duplicate submission for episode_id={episode_id}: {reason}")
        self.episode_id = episode_id
        self.reason = reason


def create_job(conn: sqlite3.Connection, url: str, *, reprocess: bool = False) -> CreateJobResult:
    """Insert a new ``queued`` job for ``url``.

    Duplicate detection + the INSERT run inside a single BEGIN IMMEDIATE
    transaction so two concurrent submits for the same URL can only result in
    one INSERT.

    Duplicate handling per build plan:
    - If an episode already exists for this URL's episode_id, reject unless
      reprocess=True (in which case proceed and update the row in place).
    - If a queued/processing job already exists for this episode_id, reject
      regardless of reprocess: don't race two pipelines against the same URL.

    Reprocess intentionally does NOT delete the existing episode row. The
    finalize stage upserts it in place (same episode_id, new pub_date,
    original created_at preserved per the build plan's timestamp semantics).
    Leaving the row in place also keeps the episode published if the
    reprocess job fails partway -- the prior audio stays live rather than
    vanishing from the feed.
    """

    episode_id = compute_episode_id(url)
    job_id = str(uuid.uuid4())

    conn.execute("BEGIN IMMEDIATE")
    try:
        in_flight = get_job_by_episode_id(conn, episode_id, statuses=("queued", "processing"))
        if in_flight is not None:
            conn.execute("ROLLBACK")
            raise DuplicateSubmissionError(
                episode_id, f"job {in_flight.id} is already {in_flight.status}"
            )

        has_episode = episode_exists(conn, episode_id)
        if has_episode and not reprocess:
            conn.execute("ROLLBACK")
            raise DuplicateSubmissionError(episode_id, "episode already exists")
        replaced = has_episode and reprocess

        conn.execute(
            "INSERT INTO jobs (id, url, episode_id, status) VALUES (?, ?, ?, 'queued')",
            (job_id, url, episode_id),
        )
        conn.execute("COMMIT")
    except DuplicateSubmissionError:
        raise
    except Exception:
        with suppress(sqlite3.OperationalError):
            conn.execute("ROLLBACK")
        raise

    job = get_job(conn, job_id)
    assert job is not None  # just inserted
    logger.info(
        "Job created",
        extra={
            "event": "job_created",
            "job_id": job_id,
            "episode_id": episode_id,
            "url": url,
            "reprocess": reprocess,
            "replaced_previous": replaced,
        },
    )
    return CreateJobResult(job=job, replaced_previous=replaced)


def claim_next_queued(conn: sqlite3.Connection) -> Job | None:
    """Atomically move the oldest queued job to ``processing`` and return it.

    Wraps SELECT + UPDATE in a single transaction so two workers (or the worker
    racing with a manual SQL edit) can't both claim the same job. Returns None
    if nothing is queued.
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE jobs SET status = 'processing', "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (row["id"],),
        )
        conn.execute("COMMIT")
    except Exception:
        # SQLite auto-aborts on commit failure; suppress the secondary
        # "no transaction is active" so the original error propagates.
        with suppress(sqlite3.OperationalError):
            conn.execute("ROLLBACK")
        raise
    return get_job(conn, row["id"])


def set_stage(conn: sqlite3.Connection, job_id: str, stage: JobStage) -> None:
    conn.execute(
        "UPDATE jobs SET stage = ?, "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
        (stage, job_id),
    )


def mark_done(conn: sqlite3.Connection, job_id: str, *, final_stage: JobStage) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'done', stage = ?, error = NULL, "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
        (final_stage, job_id),
    )


def mark_failed(conn: sqlite3.Connection, job_id: str, *, stage: JobStage, error: str) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'failed', stage = ?, error = ?, "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
        (stage, error, job_id),
    )


def job_as_dict(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "episode_id": job.episode_id,
        "url": job.url,
        "status": job.status,
        "stage": job.stage,
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
