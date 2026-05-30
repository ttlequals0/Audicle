from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.version import __version__

_ROOT = Path(__file__).resolve().parents[2]


def test_app_version_matches_version_file() -> None:
    assert __version__ == (_ROOT / "VERSION").read_text(encoding="utf-8").strip()


def test_pyprojects_in_sync_with_version_file() -> None:
    # Drift guard: scripts/sync_version.py --check exits 0 only when both
    # pyproject.toml versions equal the single-source VERSION file. If this
    # fails, run `python scripts/sync_version.py` after bumping VERSION.
    result = subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "sync_version.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
