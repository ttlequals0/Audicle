"""CRUD helpers for the ``episodes`` table.

The pipeline's finalize stage upserts a row here; the RSS render and the
media handlers read from it. ``original_url`` is the natural deduplication
key so re-running a job for the same URL updates the existing row rather
than producing a second feed entry.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from app.core.paths import file_size_or_zero
from app.services import feed_revision


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
    # Added last with defaults so existing positional/kwarg constructors keep
    # working; NULL for episodes finalized before the feature that added them.
    # (cleaned_text is write-only here -- it's fetched via get_cleaned_text(), not
    # carried on the row, so it isn't a dataclass field.)
    summary: str | None = None
    audio_size_bytes: int | None = None
    # Render counter: 1 on first finalize, +1 per reprocess. The feed folds it
    # into the GUID (only when > 1) so reprocessed episodes re-download.
    revision: int = 1
    # Source provenance (0.30.0). 'url' for the original URL-submit path (and every
    # pre-0.30.0 row); 'upload' for a directly-uploaded document, whose
    # ``original_url`` is a synthetic ``upload://`` identifier. ``source_filename``
    # is the original uploaded filename, shown in place of a source domain.
    source_type: str = "url"
    source_filename: str | None = None
    # Which reference voice narrated this episode (0.31.x), snapshotted at finalize:
    # a slot label, "Slot N", or "Default" for the legacy voice.wav. NULL only for
    # rows finalized before the column existed and not yet backfilled.
    voice_label: str | None = None
    # Podcasting 2.0 chapters document (0.51.0), stored like transcript_vtt so
    # retention and cleanup need no new handling. NULL when chapters were
    # skipped (short episode, disabled, or LLM failure).
    chapters_json: str | None = None
    generation_token: str | None = None
    audio_generation_token: str | None = None
    guid_generation_token: str | None = None


# cleaned_text is intentionally NOT in the default select: it's a large text
# body (the full article) needed only by the /media/{id}.txt route, which fetches
# it on demand via get_cleaned_text(). Loading it on every list/RSS read would
# pull megabytes the feed never uses, so it isn't an Episode field either.
_SELECT_COLUMNS = (
    "id, job_id, title, author, original_url, audio_path, artwork_path, "
    "transcript_vtt, duration_secs, pub_date, created_at, updated_at, summary, "
    "audio_size_bytes, revision, source_type, source_filename, voice_label, "
    "chapters_json, generation_token, audio_generation_token, guid_generation_token"
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
        audio_size_bytes=row["audio_size_bytes"],
        revision=row["revision"],
        source_type=row["source_type"],
        source_filename=row["source_filename"],
        voice_label=row["voice_label"],
        chapters_json=row["chapters_json"],
        generation_token=row["generation_token"],
        audio_generation_token=row["audio_generation_token"],
        guid_generation_token=row["guid_generation_token"],
    )


def audio_size(ep: Episode) -> int | None:
    """Byte size of the episode audio: the value stamped at finalize (0.6.0+),
    falling back to a stat() only for older rows whose column is NULL. Shared by
    the episodes API (``audio_size_bytes``) and the RSS enclosure length so they
    agree and neither stat()s the file on the hot path for new episodes."""

    if ep.audio_size_bytes is not None:
        return ep.audio_size_bytes
    return file_size_or_zero(ep.audio_path) if ep.audio_path else None


def ids_with_cleaned_text(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    """Subset of ``ids`` whose ``cleaned_text`` is present, without loading the
    text bodies -- used to gate the per-episode cleaned-text download link."""

    if not ids:
        return set()
    placeholders = ",".join("?" * len(ids))
    # ``!= ''`` matches the /media/{id}.txt route's ``if not cleaned_text`` 404
    # guard, so the list flag never promises a link the route would refuse.
    rows = conn.execute(
        f"SELECT id FROM episodes "
        f"WHERE cleaned_text IS NOT NULL AND cleaned_text != '' AND id IN ({placeholders})",
        ids,
    ).fetchall()
    return {row["id"] for row in rows}


def get_cleaned_text(conn: sqlite3.Connection, episode_id: str) -> str | None:
    """Fetch just the cleaned article text for one episode (the /media/{id}.txt
    body). Kept separate from the default select so the large text isn't loaded
    on every list/RSS read."""

    row = conn.execute("SELECT cleaned_text FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    return row["cleaned_text"] if row is not None else None


def set_chapters(conn: sqlite3.Connection, episode_id: str, chapters_json: str | None) -> None:
    """Store the chapters document only.

    Deliberately does not touch ``revision`` or ``updated_at``: the audio is
    unchanged, and both feed into the episode GUID, so bumping them would make
    every subscriber re-download the file over a metadata edit."""

    conn.execute("UPDATE episodes SET chapters_json = ? WHERE id = ?", (chapters_json, episode_id))
    conn.commit()


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
    cleaned_text: str | None = None,
    audio_size_bytes: int | None = None,
    source_type: str = "url",
    source_filename: str | None = None,
    voice_label: str | None = None,
    chapters_json: str | None = None,
) -> Episode:
    """Insert a new episode row, or update the existing one keyed by id.

    The update branch is the reprocess path. Per the build plan's timestamp
    semantics: ``created_at`` is left untouched (the moment the article first
    entered the feed), while ``pub_date`` is bumped to now so a reprocessed
    episode re-surfaces as new in podcast clients and re-sorts to the top of
    the feed. ``updated_at`` bumps too so RSS clients see a fresh
    ``lastBuildDate`` and because the feed versions the episode GUID by
    ``updated_at`` -- a bumped GUID is what makes clients re-download the
    regenerated audio. ``revision`` still increments here as an audit counter.
    """

    token = uuid.uuid4().hex
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
        INSERT INTO episodes (
            id, job_id, title, author, original_url, audio_path,
            artwork_path, transcript_vtt, duration_secs, summary,
            cleaned_text, audio_size_bytes, source_type, source_filename,
            voice_label, chapters_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            job_id           = excluded.job_id,
            title            = excluded.title,
            author           = excluded.author,
            original_url     = excluded.original_url,
            audio_path       = excluded.audio_path,
            artwork_path     = excluded.artwork_path,
            transcript_vtt   = excluded.transcript_vtt,
            duration_secs    = excluded.duration_secs,
            summary          = excluded.summary,
            cleaned_text     = excluded.cleaned_text,
            audio_size_bytes = excluded.audio_size_bytes,
            source_type      = excluded.source_type,
            source_filename  = excluded.source_filename,
            voice_label      = excluded.voice_label,
            chapters_json    = excluded.chapters_json,
            revision         = episodes.revision + 1,
            pub_date         = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
            updated_at       = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
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
                cleaned_text,
                audio_size_bytes,
                source_type,
                source_filename,
                voice_label,
                chapters_json,
            ),
        )
        conn.execute(
            """INSERT INTO episode_generations (
                episode_id, token, audio_path, artwork_path, transcript_vtt,
                chapters_json, cleaned_text, audio_size_bytes, duration_secs
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                id,
                token,
                audio_path,
                artwork_path,
                transcript_vtt,
                chapters_json,
                cleaned_text,
                audio_size_bytes,
                duration_secs,
            ),
        )
        conn.execute(
            "UPDATE episodes SET generation_token = ?, audio_generation_token = ?, "
            "guid_generation_token = ? WHERE id = ?",
            (token, token, token, id),
        )
        feed_revision.bump(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
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


def generation(conn: sqlite3.Connection, episode_id: str, token: str | None) -> sqlite3.Row | None:
    if token is None:
        row = conn.execute(
            "SELECT generation_token FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        token = row["generation_token"] if row else None
    if not token:
        return None
    return conn.execute(
        "SELECT * FROM episode_generations WHERE episode_id = ? AND token = ?", (episode_id, token)
    ).fetchone()


_GENERATION_VALUE_QUERIES = {
    field: (
        f"SELECT g.{field} AS value FROM episodes e "
        "JOIN episode_generations g ON g.episode_id = e.id "
        "AND g.token = COALESCE(?, e.generation_token) WHERE e.id = ?"
    )
    for field in ("audio_path", "artwork_path", "transcript_vtt", "chapters_json", "cleaned_text")
}


def generation_value(
    conn: sqlite3.Connection, episode_id: str, token: str | None, field: str
) -> object | None:
    query = _GENERATION_VALUE_QUERIES.get(field)
    if query is None:
        raise ValueError(f"unsupported generation field: {field}")
    row = conn.execute(query, (token, episode_id)).fetchone()
    return None if row is None else row["value"]


def publish_generation(
    conn: sqlite3.Connection,
    episode_id: str,
    expected_token: str | None,
    values: dict[str, object],
    *,
    token: str | None = None,
) -> str:
    token = token or uuid.uuid4().hex
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = conn.execute(
            "SELECT generation_token FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        if current is None or current["generation_token"] != expected_token:
            raise RuntimeError("episode generation changed")
        base = generation(conn, episode_id, expected_token)
        if base is None:
            base = conn.execute(
                "SELECT audio_path, artwork_path, transcript_vtt, chapters_json, cleaned_text, audio_size_bytes, duration_secs FROM episodes WHERE id = ?",
                (episode_id,),
            ).fetchone()
            if base is None:
                raise RuntimeError("current generation missing")
        merged = {
            key: values.get(key, base[key])
            for key in (
                "audio_path",
                "artwork_path",
                "transcript_vtt",
                "chapters_json",
                "cleaned_text",
                "audio_size_bytes",
                "duration_secs",
            )
        }
        conn.execute(
            "INSERT INTO episode_generations (episode_id, token, audio_path, artwork_path, transcript_vtt, chapters_json, cleaned_text, audio_size_bytes, duration_secs) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (episode_id, token, *merged.values()),
        )
        updated = conn.execute(
            "UPDATE episodes SET generation_token = ?, audio_path = ?, artwork_path = ?, transcript_vtt = ?, chapters_json = ?, cleaned_text = ?, audio_size_bytes = ?, duration_secs = ?, audio_generation_token = CASE WHEN ? THEN ? ELSE audio_generation_token END WHERE id = ? AND generation_token IS ?",
            (
                token,
                *merged.values(),
                "audio_path" in values,
                token,
                episode_id,
                expected_token,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("episode generation changed")
        feed_revision.bump(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return token


def publish_episode_generation(
    conn: sqlite3.Connection,
    *,
    token: str,
    expected_token: str | None,
    create: bool,
    id: str,
    job_id: str | None,
    original_url: str,
    title: str | None,
    author: str | None,
    audio_path: str,
    artwork_path: str | None,
    transcript_vtt: str,
    duration_secs: int,
    summary: str | None,
    cleaned_text: str | None,
    audio_size_bytes: int,
    source_type: str,
    source_filename: str | None,
    voice_label: str | None,
    chapters_json: str | None,
) -> None:
    """Atomically publish a complete generation if the episode is unchanged."""

    conn.execute("BEGIN IMMEDIATE")
    try:
        job = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None or job["status"] != "processing":
            raise RuntimeError("job is no longer processing")
        current = conn.execute(
            "SELECT generation_token FROM episodes WHERE id = ?", (id,)
        ).fetchone()
        if create:
            if current is not None:
                raise RuntimeError("episode generation changed")
            conn.execute(
                """INSERT INTO episodes (
                    id, job_id, title, author, original_url, audio_path,
                    artwork_path, transcript_vtt, duration_secs, summary,
                    cleaned_text, audio_size_bytes, source_type, source_filename,
                    voice_label, chapters_json, generation_token,
                    audio_generation_token, guid_generation_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    cleaned_text,
                    audio_size_bytes,
                    source_type,
                    source_filename,
                    voice_label,
                    chapters_json,
                    token,
                    token,
                    token,
                ),
            )
        else:
            if current is None or current["generation_token"] != expected_token:
                raise RuntimeError("episode generation changed")
            updated = conn.execute(
                """UPDATE episodes SET
                    job_id = ?, title = ?, author = ?, original_url = ?,
                    audio_path = ?, artwork_path = ?, transcript_vtt = ?,
                    duration_secs = ?, summary = ?, cleaned_text = ?,
                    audio_size_bytes = ?, source_type = ?, source_filename = ?,
                    voice_label = ?, chapters_json = ?, generation_token = ?,
                    audio_generation_token = ?, guid_generation_token = ?,
                    revision = revision + 1,
                    pub_date = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                WHERE id = ? AND generation_token = ?""",
                (
                    job_id,
                    title,
                    author,
                    original_url,
                    audio_path,
                    artwork_path,
                    transcript_vtt,
                    duration_secs,
                    summary,
                    cleaned_text,
                    audio_size_bytes,
                    source_type,
                    source_filename,
                    voice_label,
                    chapters_json,
                    token,
                    token,
                    token,
                    id,
                    expected_token,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("episode generation changed")
        conn.execute(
            """INSERT INTO episode_generations (
                episode_id, token, audio_path, artwork_path, transcript_vtt,
                chapters_json, cleaned_text, audio_size_bytes, duration_secs
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                id,
                token,
                audio_path,
                artwork_path,
                transcript_vtt,
                chapters_json,
                cleaned_text,
                audio_size_bytes,
                duration_secs,
            ),
        )
        finished = conn.execute(
            "UPDATE jobs SET status = 'done', stage = 'finalize', error = NULL, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE id = ? AND status = 'processing'",
            (job_id,),
        )
        if finished.rowcount != 1:
            raise RuntimeError("job is no longer processing")
        feed_revision.bump(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


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


def list_for_feed(conn: sqlite3.Connection) -> list[Episode]:
    """Return feed rows without materializing transcript and chapter bodies."""

    columns = _SELECT_COLUMNS.replace(
        "transcript_vtt",
        "CASE WHEN length(transcript_vtt) > 0 THEN '1' END AS transcript_vtt",
    ).replace("chapters_json", "CASE WHEN length(chapters_json) > 0 THEN '1' END AS chapters_json")
    rows = conn.execute(
        "SELECT " + columns + " FROM episodes WHERE audio_path IS NOT NULL "
        "ORDER BY pub_date DESC, created_at DESC"
    ).fetchall()
    return [_row_to_episode(row) for row in rows]


def _search_clause(q: str | None) -> tuple[str, list[str]]:
    """SQL fragment and params restricting a listing to rows matching ``q``.

    Empty or whitespace-only input means no filter, so the count and the page
    can never disagree about what they are counting. The term is matched as
    literal text: ``%`` and ``_`` typed into the search box are escaped so they
    do not act as wildcards. Backslash is escaped first, otherwise the escapes
    added for ``%`` and ``_`` would themselves get escaped.
    """

    term = (q or "").strip()
    if not term:
        return "", []
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    clause = (
        " AND (title LIKE ? ESCAPE '\\'"
        " OR original_url LIKE ? ESCAPE '\\'"
        " OR source_filename LIKE ? ESCAPE '\\')"
    )
    return clause, [like, like, like]


def count_published(conn: sqlite3.Connection, *, q: str | None = None) -> int:
    clause, params = _search_clause(q)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM episodes WHERE audio_path IS NOT NULL" + clause,
        params,
    ).fetchone()
    return int(row["n"] if row else 0)


def list_published_page(
    conn: sqlite3.Connection, *, limit: int, offset: int, q: str | None = None
) -> list[Episode]:
    """SQL-paginated counterpart to ``list_published`` -- avoids reading every
    row when the admin UI only wants one page. ``q`` optionally filters on
    title, source URL, or uploaded filename."""

    clause, params = _search_clause(q)
    rows = conn.execute(
        # _SELECT_COLUMNS and the search clause are fixed module text; the
        # search term itself is bound, never interpolated.
        "SELECT " + _SELECT_COLUMNS + " "
        "FROM episodes "
        "WHERE audio_path IS NOT NULL" + clause + " "
        "ORDER BY pub_date DESC, created_at DESC "
        "LIMIT ? OFFSET ?",
        [*params, limit, offset],
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
