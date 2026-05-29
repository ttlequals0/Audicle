from __future__ import annotations

import logging
from pathlib import Path

from app import startup

_LOG = logging.getLogger("test")


def test_packaged_defaults_match_shipped_live_files() -> None:
    """defaults/ is the seed source for empty bind-mounts; the shipped live
    files (used directly on a non-mounted deploy) must stay byte-identical so
    the two copies can't silently drift."""

    app_dir = startup._APP_DIR
    assert (app_dir / "defaults" / "script.txt").read_text() == (
        app_dir / "prompts" / "script.txt"
    ).read_text()
    assert (app_dir / "defaults" / "pronunciation.json").read_text() == (
        app_dir / "corrections" / "pronunciation.json"
    ).read_text()


def test_seed_if_missing_fills_empty_target(tmp_path: Path) -> None:
    src = tmp_path / "src" / "script.txt"
    src.parent.mkdir()
    src.write_text("DEFAULT PROMPT\n", encoding="utf-8")
    dst = tmp_path / "mount" / "script.txt"  # missing dir + file (empty bind-mount)

    startup._seed_if_missing(src, dst, _LOG)

    assert dst.read_text() == "DEFAULT PROMPT\n"


def test_seed_if_missing_does_not_overwrite_existing(tmp_path: Path) -> None:
    src = tmp_path / "src" / "pronunciation.json"
    src.parent.mkdir()
    src.write_text('{"API": "A.P.I."}\n', encoding="utf-8")
    dst = tmp_path / "mount" / "pronunciation.json"
    dst.parent.mkdir()
    dst.write_text("{}\n", encoding="utf-8")  # operator emptied it

    startup._seed_if_missing(src, dst, _LOG)

    assert dst.read_text() == "{}\n"


def test_seed_defaults_wires_prompt_corrections_and_artwork(
    tmp_path: Path, env: Path, monkeypatch
) -> None:
    """_seed_defaults seeds all three packaged defaults into their locations."""

    app_dir = tmp_path / "app"
    (app_dir / "defaults").mkdir(parents=True)
    (app_dir / "assets").mkdir(parents=True)
    (app_dir / "prompts").mkdir(parents=True)
    (app_dir / "corrections").mkdir(parents=True)
    (app_dir / "defaults" / "script.txt").write_text("P\n", encoding="utf-8")
    (app_dir / "defaults" / "pronunciation.json").write_text("{}\n", encoding="utf-8")
    (app_dir / "assets" / "default-artwork.jpg").write_bytes(b"\xff\xd8JPEG")
    monkeypatch.setattr(startup, "_APP_DIR", app_dir)

    from app.config import get_settings

    startup._seed_defaults(get_settings(), _LOG)

    assert (app_dir / "prompts" / "script.txt").exists()
    assert (app_dir / "corrections" / "pronunciation.json").exists()
    assert (env / "media" / "default.jpg").read_bytes() == b"\xff\xd8JPEG"
