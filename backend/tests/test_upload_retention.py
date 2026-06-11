from __future__ import annotations

from pathlib import Path

from app.config import get_settings
from app.core import database
from app.core.paths import media_dir
from app.services import episodes, retention


def _seed(env: Path, *, id_: str, filename: str) -> None:
    media = media_dir(get_settings())
    media.mkdir(parents=True, exist_ok=True)
    (media / f"{id_}.mp3").write_bytes(b"FAKE")
    (media / f"{id_}.source{Path(filename).suffix}").write_bytes(b"DOC")
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id=id_,
            job_id=None,
            original_url=f"upload://hash/{filename}",
            title=id_,
            author="A",
            audio_path=str(media / f"{id_}.mp3"),
            artwork_path=None,
            transcript_vtt="WEBVTT\n",
            duration_secs=10,
            source_type="upload",
            source_filename=filename,
        )
    finally:
        conn.close()


def test_purge_removes_uploaded_source_file(env: Path) -> None:
    database.run_migrations(env)
    _seed(env, id_="old", filename="paper.pdf")
    media = media_dir(get_settings())
    assert (media / "old.source.pdf").exists()

    # older_than_days=0 is the wipe-all contract.
    result = retention.purge_older_than(get_settings(), 0)

    assert result.rows_deleted == 1
    assert not (media / "old.source.pdf").exists()
    assert not (media / "old.mp3").exists()
    conn = database.connect(database.db_path(env))
    try:
        assert episodes.get_by_id(conn, "old") is None
    finally:
        conn.close()


def test_orphan_sweep_preserves_live_source_and_reaps_orphan(env: Path) -> None:
    database.run_migrations(env)
    _seed(env, id_="live", filename="kept.docx")
    media = media_dir(get_settings())
    # An orphan upload original with no matching episode row.
    (media / "orphan.source.pdf").write_bytes(b"DOC")

    removed = retention.sweep_orphan_media(get_settings())

    assert (media / "live.source.docx").exists()  # live episode's original survives
    assert not (media / "orphan.source.pdf").exists()
    assert removed == 1
