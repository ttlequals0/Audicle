"""Single source of truth for the app version: the repo-root ``VERSION`` file.

The backend is a uv *virtual* package (run from source, not pip-installed), so
we read the ``VERSION`` file directly -- the Dockerfile copies it to
``/app/VERSION`` (one dir above this package) and it sits at the repo root in
development. ``scripts/sync_version.py`` propagates the same value into the
pyproject files; a test guards against drift.
"""

from __future__ import annotations

from pathlib import Path


def _read_version() -> str:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "VERSION"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    return "0.0.0"


__version__ = _read_version()
