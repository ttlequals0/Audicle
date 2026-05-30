"""CRUD helpers for the ``episodes`` table.

The pipeline's finalize stage upserts a row here; the RSS render and the
media handlers read from it. ``original_url`` is the natural deduplication
key so re-running a job for the same URL updates the existing row rather
than producing a second feed entry.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Episode:
    id: str
    job_id: str | None
    title: str | None
    author: str | None
    original_url: str
    audio_path: str | None
    artwork_path: str | None
    transcript_vtt: str | None
    duration_secs: int | None
    pub_date: str
    created_at: str
    updated_at: str
    # Added last with a default so existing positional/kwarg constructors keep
    # working; NULL for episodes finalized before the summary feature.
    summary: str | None = None


_SELECT_COLUMNS = (
    "id, job_id, title, author, original_url, audio_path, artwork_path, "
    "transcript_vtt, duration_secs, pub_date, created_at, updated_at, summary"
)


def _row_to_episode(row: sqlite3.Row) -> Episode:
    return Episode(
        id=row["id"],
        job_id=row["job_id"],
        title=row["title"],
        author=row["author"],
        original_url=row["original_url"],
        audio_path=row["audio_path"],
        artwork_path=row["artwork_path"],
        transcript_vtt=row["transcript_vtt"],
        duration_secs=row["duration_secs"],
        pub_date=row["pub_date"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        summary=row["summary"],
    )


def upsert(
    conn: sqlite3.Connection,
    *,
    id: str,
    job_id: str | None,
    original_url: str,
    title: str | None,
    author: str | None,
    audio_path: str | None,
    artwork_path: str | None,
    transcript_vtt: str | None,
    duration_secs: int | None,
    summary: str | None = None,
) -> Episode:
    """Insert a new episode row, or update the existing one keyed by id.

    The update branch is the reprocess path. Per the build plan's timestamp
    semantics: ``created_at`` is left untouched (the moment the article first
    entered the feed), while ``pub_date`` is bumped to now so a reprocessed
    episode re-surfaces as new in podcast clients and re-sorts to the top of
    the feed. ``updated_at`` bumps too so RSS clients see a fresh
    ``lastBuildDate``.
    """

    conn.execute(
        """
        INSERT INTO episodes (
            id, job_id, title, author, original_url, audio_path,
            artwork_path, transcript_vtt, duration_secs, summary
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            job_id          = excluded.job_id,
            title           = excluded.title,
            author          = excluded.author,
            original_url    = excluded.original_url,
            audio_path      = excluded.audio_path,
            artwork_path    = excluded.artwork_path,
            transcript_vtt  = excluded.transcript_vtt,
            duration_secs   = excluded.duration_secs,
            summary         = excluded.summary,
            pub_date        = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
            updated_at      = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        """,
        (
            id,
            job_id,
            title,
            author,
            original_url,
            audio_path,
            artwork_path,
            transcript_vtt,
            duration_secs,
            summary,
        ),
    )
    conn.commit()
    # _SELECT_COLUMNS is a fixed module constant -- no user input.
    row = conn.execute(
        "SELECT " + _SELECT_COLUMNS + " FROM episodes WHERE id = ?",
        (id,),
    ).fetchone()
    if row is None:
        # ``assert`` would disappear under ``python -O``; a real check stays.
        raise RuntimeError(f"episode {id!r} disappeared between upsert and SELECT")
    return _row_to_episode(row)


def get_by_id(conn: sqlite3.Connection, episode_id: str) -> Episode | None:
    # _SELECT_COLUMNS is a fixed module constant -- no user input.
    row = conn.execute(
        "SELECT " + _SELECT_COLUMNS + " FROM episodes WHERE id = ?",
        (episode_id,),
    ).fetchone()
    return None if row is None else _row_to_episode(row)


def list_published(conn: sqlite3.Connection) -> list[Episode]:
    """Return episodes in newest-first order for RSS rendering.

    Filters to rows that have a non-NULL ``audio_path`` so a half-finalized
    row (audio still pending) doesn't leak into the feed.
    """

    rows = conn.execute(
        # _SELECT_COLUMNS is a fixed module constant -- no user input.
        "SELECT " + _SELECT_COLUMNS + " "
        "FROM episodes "
        "WHERE audio_path IS NOT NULL "
        "ORDER BY pub_date DESC, created_at DESC"
    ).fetchall()
    return [_row_to_episode(row) for row in rows]


def count_published(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM episodes WHERE audio_path IS NOT NULL").fetchone()
    return int(row["n"] if row else 0)


def list_published_page(conn: sqlite3.Connection, *, limit: int, offset: int) -> list[Episode]:
    """SQL-paginated counterpart to ``list_published`` -- avoids reading
    every row when the admin UI only wants 50."""

    rows = conn.execute(
        # _SELECT_COLUMNS is a fixed module constant -- no user input.
        "SELECT " + _SELECT_COLUMNS + " "
        "FROM episodes "
        "WHERE audio_path IS NOT NULL "
        "ORDER BY pub_date DESC, created_at DESC "
        "LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [_row_to_episode(row) for row in rows]


def latest_updated_at(conn: sqlite3.Connection) -> str | None:
    """Most-recent ``updated_at`` across published episodes, for the RSS
    ``Last-Modified`` header and the ``<lastBuildDate>`` channel field."""

    row = conn.execute(
        """
        SELECT updated_at
        FROM episodes
        WHERE audio_path IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT 1
        """
    ).fetchone()
    return None if row is None else row["updated_at"]
