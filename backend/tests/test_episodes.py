from __future__ import annotations

from pathlib import Path

from app.core import database
from app.services import episodes


def _open(env: Path):
    database.run_migrations(env)
    return database.connect(database.db_path(env))


def test_upsert_inserts_then_updates_same_id(env: Path) -> None:
    conn = _open(env)
    try:
        first = episodes.upsert(
            conn,
            id="ep1",
            job_id=None,
            original_url="https://example.test/a",
            title="First",
            author="Alice",
            audio_path="/data/media/ep1.mp3",
            artwork_path="/data/media/ep1.jpg",
            transcript_vtt="WEBVTT\n",
            duration_secs=120,
        )
        assert first.id == "ep1"
        assert first.title == "First"
        assert first.duration_secs == 120

        second = episodes.upsert(
            conn,
            id="ep1",
            job_id=None,
            original_url="https://example.test/a",
            title="First (updated)",
            author="Alice",
            audio_path="/data/media/ep1.mp3",
            artwork_path=None,  # artwork fell back this run
            transcript_vtt="WEBVTT\n\n1\n00:00:00.000 --> 00:00:05.000\nhello\n",
            duration_secs=130,
        )
        assert second.title == "First (updated)"
        assert second.artwork_path is None
        assert second.duration_secs == 130
        # created_at is the original feed-entry moment and must survive reprocess.
        assert second.created_at == first.created_at
        # pub_date bumps on reprocess so the episode re-surfaces as new (>= original;
        # equal is possible when both upserts land in the same wall-clock second).
        assert second.pub_date >= first.pub_date
    finally:
        conn.close()


def test_list_published_orders_newest_first(env: Path) -> None:
    conn = _open(env)
    try:
        for n in range(3):
            episodes.upsert(
                conn,
                id=f"ep{n}",
                job_id=None,
                original_url=f"https://example.test/{n}",
                title=f"Episode {n}",
                author="A",
                audio_path=f"/data/media/ep{n}.mp3",
                artwork_path=None,
                transcript_vtt="WEBVTT\n",
                duration_secs=10 * n,
            )
        # Manually bump the pub_date on ep1 so we have a deterministic order
        # independent of insert clock resolution.
        conn.execute("UPDATE episodes SET pub_date = '2026-06-01T00:00:00Z' WHERE id = 'ep1'")
        conn.execute("UPDATE episodes SET pub_date = '2026-05-01T00:00:00Z' WHERE id = 'ep0'")
        conn.execute("UPDATE episodes SET pub_date = '2026-04-01T00:00:00Z' WHERE id = 'ep2'")

        listed = episodes.list_published(conn)
        assert [ep.id for ep in listed] == ["ep1", "ep0", "ep2"]
    finally:
        conn.close()


def test_list_published_excludes_rows_without_audio(env: Path) -> None:
    """A row created during finalize-failure recovery (audio_path NULL) must
    not leak into the RSS feed."""

    conn = _open(env)
    try:
        episodes.upsert(
            conn,
            id="published",
            job_id=None,
            original_url="https://example.test/p",
            title="Published",
            author="A",
            audio_path="/data/media/published.mp3",
            artwork_path=None,
            transcript_vtt="WEBVTT\n",
            duration_secs=10,
        )
        episodes.upsert(
            conn,
            id="half",
            job_id=None,
            original_url="https://example.test/h",
            title="Half-finalized",
            author="A",
            audio_path=None,
            artwork_path=None,
            transcript_vtt=None,
            duration_secs=None,
        )
        listed = episodes.list_published(conn)
        assert [ep.id for ep in listed] == ["published"]
    finally:
        conn.close()


def test_get_by_id_returns_none_for_unknown(env: Path) -> None:
    conn = _open(env)
    try:
        assert episodes.get_by_id(conn, "missing") is None
    finally:
        conn.close()


def test_latest_updated_at_tracks_most_recent_published(env: Path) -> None:
    conn = _open(env)
    try:
        episodes.upsert(
            conn,
            id="a",
            job_id=None,
            original_url="https://example.test/a",
            title=None,
            author=None,
            audio_path="/data/media/a.mp3",
            artwork_path=None,
            transcript_vtt=None,
            duration_secs=None,
        )
        conn.execute("UPDATE episodes SET updated_at = '2026-06-01T00:00:00Z' WHERE id = 'a'")
        assert episodes.latest_updated_at(conn) == "2026-06-01T00:00:00Z"
    finally:
        conn.close()
