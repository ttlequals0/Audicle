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
        assert first.revision == 1

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
        # revision increments on the reprocess branch so the feed hands out a new GUID.
        assert second.revision == 2
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


def _seed_searchable(
    conn,
    *,
    id_: str,
    title: str,
    url: str,
    filename: str | None = None,
) -> None:
    episodes.upsert(
        conn,
        id=id_,
        job_id=None,
        original_url=url,
        title=title,
        author="A",
        audio_path=f"/data/media/{id_}.mp3",
        artwork_path=None,
        transcript_vtt=None,
        duration_secs=10,
        source_type="upload" if filename else "url",
        source_filename=filename,
    )


def test_search_matches_title(env: Path) -> None:
    conn = _open(env)
    try:
        _seed_searchable(conn, id_="a", title="Rust memory model", url="https://x.test/a")
        _seed_searchable(conn, id_="b", title="Python packaging", url="https://x.test/b")
        found = episodes.list_published_page(conn, limit=50, offset=0, q="memory")
        assert [ep.id for ep in found] == ["a"]
    finally:
        conn.close()


def test_search_matches_original_url(env: Path) -> None:
    conn = _open(env)
    try:
        _seed_searchable(conn, id_="a", title="One", url="https://wired.test/story")
        _seed_searchable(conn, id_="b", title="Two", url="https://other.test/story")
        found = episodes.list_published_page(conn, limit=50, offset=0, q="wired")
        assert [ep.id for ep in found] == ["a"]
    finally:
        conn.close()


def test_search_matches_source_filename(env: Path) -> None:
    conn = _open(env)
    try:
        _seed_searchable(
            conn,
            id_="a",
            title="Untitled",
            url="upload://abc",
            filename="quarterly-report.pdf",
        )
        _seed_searchable(conn, id_="b", title="Untitled", url="upload://def", filename="notes.md")
        found = episodes.list_published_page(conn, limit=50, offset=0, q="quarterly")
        assert [ep.id for ep in found] == ["a"]
    finally:
        conn.close()


def test_search_is_case_insensitive(env: Path) -> None:
    conn = _open(env)
    try:
        _seed_searchable(conn, id_="a", title="Rust Memory Model", url="https://x.test/a")
        found = episodes.list_published_page(conn, limit=50, offset=0, q="MEMORY")
        assert [ep.id for ep in found] == ["a"]
    finally:
        conn.close()


def test_search_treats_wildcards_as_literal_text(env: Path) -> None:
    conn = _open(env)
    try:
        _seed_searchable(conn, id_="a", title="Up 50% year on year", url="https://x.test/a")
        _seed_searchable(conn, id_="b", title="No percentage here", url="https://x.test/b")
        hits = episodes.list_published_page(conn, limit=50, offset=0, q="50%")
        assert [ep.id for ep in hits] == ["a"]
        # A bare wildcard matches only the row that literally contains it,
        # rather than matching everything the way an unescaped % would.
        bare = episodes.list_published_page(conn, limit=50, offset=0, q="%")
        assert [ep.id for ep in bare] == ["a"]
        assert episodes.list_published_page(conn, limit=50, offset=0, q="_") == []
    finally:
        conn.close()


def test_blank_search_is_no_filter(env: Path) -> None:
    conn = _open(env)
    try:
        _seed_searchable(conn, id_="a", title="One", url="https://x.test/a")
        _seed_searchable(conn, id_="b", title="Two", url="https://x.test/b")
        for blank in (None, "", "   "):
            assert episodes.count_published(conn, q=blank) == 2
            assert len(episodes.list_published_page(conn, limit=50, offset=0, q=blank)) == 2
    finally:
        conn.close()


def test_count_and_page_agree_on_the_same_term(env: Path) -> None:
    conn = _open(env)
    try:
        for n in range(5):
            _seed_searchable(
                conn, id_=f"hit{n}", title=f"Rust part {n}", url=f"https://x.test/{n}"
            )
        _seed_searchable(conn, id_="miss", title="Python", url="https://x.test/p")
        assert episodes.count_published(conn, q="rust") == 5
        assert len(episodes.list_published_page(conn, limit=50, offset=0, q="rust")) == 5
    finally:
        conn.close()
