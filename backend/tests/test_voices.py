from __future__ import annotations

from pathlib import Path

import pytest
from app.core import database
from app.services import voices


@pytest.fixture
def _slots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the voices dir at a temp folder so tests don't touch the repo."""

    d = tmp_path / "voices"
    d.mkdir()
    monkeypatch.setattr(voices, "voices_dir", lambda: d)
    return d


def _fill(d: Path, *slots: int) -> None:
    for n in slots:
        (d / f"slot{n}.wav").write_bytes(b"RIFFFAKEWAVE")


def test_filled_slots(_slots: Path) -> None:
    assert voices.filled_slots() == []
    _fill(_slots, 2, 4)
    assert voices.filled_slots() == [2, 4]


def test_resolve_no_slots_is_legacy_none(env: Path, _slots: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        assert voices.resolve(conn, "random") is None
        assert voices.resolve(conn, "2") is None


def test_resolve_forced_slot(env: Path, _slots: Path) -> None:
    database.run_migrations(env)
    _fill(_slots, 1, 3)
    with database.connection(env) as conn:
        assert voices.resolve(conn, "3") == "3"
        # A forced-but-empty slot falls back to a filled one.
        assert voices.resolve(conn, "5") in {"1", "3"}


def test_resolve_random_picks_filled(env: Path, _slots: Path) -> None:
    database.run_migrations(env)
    _fill(_slots, 2)
    with database.connection(env) as conn:
        assert voices.resolve(conn, "random") == "2"
        assert voices.resolve(conn, None) == "2"


def test_resolve_last_used(env: Path, _slots: Path) -> None:
    database.run_migrations(env)
    _fill(_slots, 1, 4)
    with database.connection(env) as conn:
        conn.execute(
            "INSERT INTO jobs (id, url, episode_id, status, voice_id) "
            "VALUES ('j1', 'u', 'e1', 'done', '4')"
        )
        conn.commit()
        assert voices.resolve(conn, "last") == "4"


def test_labels_round_trip(env: Path, _slots: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        voices.set_label(conn, 1, "Morgan")
        voices.set_label(conn, 2, "Alex")
        assert voices.get_labels(conn) == {"1": "Morgan", "2": "Alex"}
        voices.set_label(conn, 1, "")  # clearing removes it
        assert voices.get_labels(conn) == {"2": "Alex"}
