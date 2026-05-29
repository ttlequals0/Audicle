from __future__ import annotations

from pathlib import Path

from app.config import get_settings
from app.core import database
from app.core.paths import media_dir
from app.services import episodes, retention


def test_orphan_sweep_removes_files_with_no_episode_row(env: Path) -> None:
    database.run_migrations(env)
    media = media_dir(get_settings())
    media.mkdir(parents=True, exist_ok=True)

    # Create a live episode + matching files (should NOT be removed).
    (media / "live.mp3").write_bytes(b"FAKE")
    (media / "live.jpg").write_bytes(b"FAKE")
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id="live",
            job_id=None,
            original_url="https://example.test/live",
            title="Live",
            author="A",
            audio_path=str(media / "live.mp3"),
            artwork_path=str(media / "live.jpg"),
            transcript_vtt="WEBVTT\n",
            duration_secs=10,
        )
    finally:
        conn.close()

    # Orphan files with no matching row (should be removed).
    (media / "orphan.mp3").write_bytes(b"FAKE")
    (media / "orphan.jpg").write_bytes(b"FAKE")
    (media / "orphan_combined.wav").write_bytes(b"FAKE")

    removed = retention.sweep_orphan_media(get_settings())
    assert removed == 3
    assert (media / "live.mp3").exists()
    assert (media / "live.jpg").exists()
    assert not (media / "orphan.mp3").exists()
    assert not (media / "orphan.jpg").exists()
    assert not (media / "orphan_combined.wav").exists()


def test_orphan_sweep_skips_voice_reference(env: Path) -> None:
    database.run_migrations(env)
    media = media_dir(get_settings())
    media.mkdir(parents=True, exist_ok=True)
    voice = media / "voice.wav"
    voice.write_bytes(b"VOICE")
    removed = retention.sweep_orphan_media(get_settings())
    assert removed == 0
    assert voice.exists()


def test_orphan_sweep_is_no_op_when_media_dir_missing(env: Path) -> None:
    database.run_migrations(env)
    removed = retention.sweep_orphan_media(get_settings())
    assert removed == 0
