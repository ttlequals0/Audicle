"""Extract release notes for a version from CHANGELOG.md.

Used by ``scripts/publish_release.sh`` to build a GitHub release body from the
changelog, so the notes and the changelog can never disagree.

``--rollup-since PREV`` gathers every section newer than ``PREV`` up to and
including the requested version. A branch that bumps the version more than once
before merging (a fix release on top of a feature release, say) would otherwise
publish only its last section and ship the rest undocumented.

Usage:
    python3 scripts/changelog_section.py 0.55.4
    python3 scripts/changelog_section.py 0.55.4 --rollup-since 0.53.1
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# "## [0.55.4] - 2026-08-12"
_HEADING = re.compile(r"^## \[([0-9][^\]]*)\](.*)$")


def _sections(text: str) -> list[tuple[str, str]]:
    """Every version section as (version, body), newest first as written."""

    out: list[tuple[str, str]] = []
    current: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            if current is not None:
                out.append((current, "\n".join(lines).strip()))
            current = match.group(1)
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        out.append((current, "\n".join(lines).strip()))
    return out


def _version_key(version: str) -> tuple[int, ...]:
    """Sortable key. Non-numeric parts sort as 0 rather than raising, so a
    suffixed version never crashes the release script."""

    parts: list[int] = []
    for piece in version.split("."):
        digits = re.match(r"\d+", piece)
        parts.append(int(digits.group()) if digits else 0)
    return tuple(parts)


def build_notes(text: str, version: str, rollup_since: str | None) -> str:
    sections = dict(_sections(text))
    if version not in sections:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")

    wanted = [version]
    if rollup_since:
        target = _version_key(version)
        previous = _version_key(rollup_since)
        wanted = [
            found
            for found in sections
            if previous < _version_key(found) <= target
        ]
        wanted.sort(key=_version_key, reverse=True)

    chunks = []
    for name in wanted:
        body = sections[name]
        # Heading per section only when rolling up several; a single section
        # needs no header because the release title already carries the version.
        chunks.append(f"## {name}\n\n{body}" if len(wanted) > 1 else body)
    return "\n\n".join(chunks).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--rollup-since", default=None)
    parser.add_argument("--changelog", default="CHANGELOG.md")
    args = parser.parse_args()

    text = Path(args.changelog).read_text(encoding="utf-8")
    print(build_notes(text, args.version, args.rollup_since))
    return 0


if __name__ == "__main__":
    sys.exit(main())
