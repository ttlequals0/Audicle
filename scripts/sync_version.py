#!/usr/bin/env python3
"""Sync VERSION into the app and sidecar pyproject.toml files.

Run after bumping VERSION. --check reports drift without writing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_VERSION_FILE = _ROOT / "VERSION"
_PYPROJECTS = (
    _ROOT / "pyproject.toml",
    _ROOT / "tts-wrapper" / "pyproject.toml",
    _ROOT / "render" / "pyproject.toml",
)
# Match the first version field, which belongs to [project].
_VERSION_RE = re.compile(r'^version = "[^"]*"', re.MULTILINE)


def _current(pyproject: Path) -> str | None:
    match = re.search(r'^version = "([^"]*)"', pyproject.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else None


def main() -> int:
    check = "--check" in sys.argv[1:]
    version = _VERSION_FILE.read_text(encoding="utf-8").strip()
    drift = []
    for pyproject in _PYPROJECTS:
        current = _current(pyproject)
        if current == version:
            continue
        if check:
            drift.append(f"{pyproject.relative_to(_ROOT)}: {current} != VERSION {version}")
            continue
        text = pyproject.read_text(encoding="utf-8")
        pyproject.write_text(_VERSION_RE.sub(f'version = "{version}"', text, count=1), encoding="utf-8")
        print(f"synced {pyproject.relative_to(_ROOT)} -> {version}")
    if check and drift:
        print("VERSION drift (run scripts/sync_version.py):", *drift, sep="\n  ", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
