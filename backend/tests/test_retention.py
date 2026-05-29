from __future__ import annotations

from pathlib import Path

from app.config import get_settings
from app.core import database
from app.core.paths import media_dir
from app.services import episodes, retention


def _seed(env: Path, *, id_: str, pub_date: str, with_files: bool = True) -> Path:
    database.run_migrations(env)
    media = media_dir(get_settings())
    media.mkdir(parents=True, exist_ok=True)
    mp3 = media / f"{id_}.mp3"
    jpg = media / f"{id_}.jpg"
    if with_files:
        mp3.write_bytes(b"FAKE_MP3")
        jpg.write_bytes(b"FAKE_JPG")
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id=id_,
            job_id=None,
            original_url=f"https://example.test/{id_}",
            title=id_,
            author="A",
            audio_path=str(mp3),
            artwork_path=str(jpg) if with_files else None,
            transcript_vtt="WEBVTT\n",
            duration_secs=10,
        )
        conn.execute("UPDATE episodes SET pub_date=? WHERE id=?", (pub_date, id_))
        conn.commit()
    finally:
        conn.close()
    return media


def test_purge_older_than_removes_old_rows_and_files(env: Path) -> None:
    media = _seed(env, id_="old", pub_date="2020-01-01T00:00:00Z")
    _seed(env, id_="new", pub_date="2099-01-01T00:00:00Z")

    result = retention.purge_older_than(get_settings(), older_than_days=30)

    assert result.rows_deleted == 1
    assert result.episode_ids == ("old",)
    assert result.files_removed == 2  # mp3 + jpg
    assert not (media / "old.mp3").exists()
    assert not (media / "old.jpg").exists()
    assert (media / "new.mp3").exists()
    assert (media / "new.jpg").exists()

    conn = database.connect(database.db_path(env))
    try:
        assert episodes.get_by_id(conn, "old") is None
        assert episodes.get_by_id(conn, "new") is not None
    finally:
        conn.close()


def test_purge_expired_jobs_reaps_old_terminal_unreferenced(env: Path) -> None:
    """Old done/failed job rows with no live episode reference are reaped;
    queued jobs, recent jobs, and jobs a live episode points at survive."""

    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        # old, terminal, unreferenced -> reaped
        conn.execute(
            "INSERT INTO jobs (id, url, episode_id, status, created_at) "
            "VALUES ('j_old', 'u', 'e_old', 'done', '2020-01-01T00:00:00Z')"
        )
        # old but still queued -> kept (never drop an in-flight job)
        conn.execute(
            "INSERT INTO jobs (id, url, episode_id, status, created_at) "
            "VALUES ('j_queued', 'u', 'e_q', 'queued', '2020-01-01T00:00:00Z')"
        )
        # old, terminal, but referenced by a live episode -> kept (FK + provenance)
        conn.execute(
            "INSERT INTO jobs (id, url, episode_id, status, created_at) "
            "VALUES ('j_ref', 'u', 'e_ref', 'done', '2020-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO episodes (id, job_id, original_url) VALUES ('e_ref', 'j_ref', 'u2')"
        )
        # recent terminal -> kept (inside the window)
        conn.execute(
            "INSERT INTO jobs (id, url, episode_id, status, created_at) "
            "VALUES ('j_new', 'u', 'e_new', 'failed', '2099-01-01T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()

    deleted = retention.purge_expired_jobs(get_settings(), older_than_days=30)
    assert deleted == 1

    conn = database.connect(database.db_path(env))
    try:
        survivors = {row["id"] for row in conn.execute("SELECT id FROM jobs").fetchall()}
    finally:
        conn.close()
    assert survivors == {"j_queued", "j_ref", "j_new"}


def test_purge_with_zero_days_wipes_everything(env: Path) -> None:
    _seed(env, id_="recent", pub_date="2099-01-01T00:00:00Z")
    result = retention.purge_older_than(get_settings(), older_than_days=0)
    assert result.rows_deleted == 1


def test_purge_no_op_when_nothing_matches(env: Path) -> None:
    _seed(env, id_="recent", pub_date="2099-01-01T00:00:00Z")
    result = retention.purge_older_than(get_settings(), older_than_days=10000)
    assert result.rows_deleted == 0
    assert result.files_removed == 0
    assert result.episode_ids == ()


def test_purge_silently_skips_missing_files(env: Path) -> None:
    """A row whose mp3/jpg already vanished (manual cleanup, restore from
    backup, etc.) must still delete the DB row and not error."""

    media = _seed(env, id_="orphan", pub_date="2020-01-01T00:00:00Z")
    (media / "orphan.mp3").unlink()
    (media / "orphan.jpg").unlink()
    result = retention.purge_older_than(get_settings(), older_than_days=30)
    assert result.rows_deleted == 1
    assert result.files_removed == 0


def test_purge_refuses_paths_outside_media_dir(
    env: Path,
    tmp_path: Path,
    caplog,
) -> None:
    """A poisoned row pointing audio_path at /etc/passwd must NOT cause
    the sweep to remove that file. The sweep logs `retention_unsafe_path`
    so operators can grep for the breakout attempt."""

    import logging

    database.run_migrations(env)
    target = tmp_path / "victim.txt"
    target.write_text("important")
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id="evil",
            job_id=None,
            original_url="https://example.test/evil",
            title="evil",
            author="A",
            audio_path=str(target),
            artwork_path=None,
            transcript_vtt=None,
            duration_secs=10,
        )
        conn.execute("UPDATE episodes SET pub_date='2020-01-01T00:00:00Z' WHERE id='evil'")
        conn.commit()
    finally:
        conn.close()
    with caplog.at_level(logging.WARNING, logger="app.services.retention"):
        result = retention.purge_older_than(get_settings(), older_than_days=30)
    assert result.rows_deleted == 1
    assert target.exists(), "must NOT delete files outside media_dir"
    assert any(getattr(rec, "event", "") == "retention_unsafe_path" for rec in caplog.records), (
        "must log retention_unsafe_path so operators can see the breakout attempt"
    )


def test_purge_negative_older_than_days_raises_value_error(env: Path) -> None:
    import pytest

    with pytest.raises(ValueError, match="older_than_days"):
        retention.purge_older_than(get_settings(), older_than_days=-1)


def test_purge_huge_older_than_days_rejected_before_overflow(env: Path) -> None:
    """Above ``_MAX_OLDER_THAN_DAYS`` we raise rather than letting
    ``timedelta(days=N)`` overflow Python's year-9999 limit."""

    import pytest

    with pytest.raises(ValueError, match="older_than_days"):
        retention.purge_older_than(get_settings(), older_than_days=1_000_000)
