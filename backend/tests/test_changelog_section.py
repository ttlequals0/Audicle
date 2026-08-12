"""Release-notes extraction for scripts/publish_release.sh.

The script turns a CHANGELOG section into a GitHub release body. If that
extraction is wrong the release page misdescribes what shipped, which is worse
than having no release page, so the behaviour is pinned here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "changelog_section.py"
_spec = importlib.util.spec_from_file_location("changelog_section", _SCRIPT)
assert _spec and _spec.loader
changelog_section = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(changelog_section)

SAMPLE = """# Changelog

Preamble that is not a version section.

## [Unreleased]

## [0.55.4] - 2026-08-12

### Changed

- Newest thing.

## [0.55.3] - 2026-08-12

### Fixed

- Middle thing.

## [0.54.0] - 2026-08-11

### Added

- Older thing.
"""


def test_single_section_has_no_version_heading() -> None:
    """The release title already carries the version, so a lone section should
    not repeat it."""

    notes = changelog_section.build_notes(SAMPLE, "0.55.4", None)
    assert "Newest thing." in notes
    assert "Middle thing." not in notes
    assert not notes.startswith("## ")


def test_rollup_gathers_every_section_since_the_previous_release() -> None:
    """A branch that bumps the version several times before merging must not
    publish only its last section and ship the rest undocumented."""

    notes = changelog_section.build_notes(SAMPLE, "0.55.4", "0.54.0")
    assert "## 0.55.4" in notes
    assert "## 0.55.3" in notes
    # The floor is exclusive: 0.54.0 already shipped under its own release.
    assert "## 0.54.0" not in notes
    assert "Older thing." not in notes


def test_rollup_orders_newest_first() -> None:
    notes = changelog_section.build_notes(SAMPLE, "0.55.4", "0.53.0")
    assert notes.index("## 0.55.4") < notes.index("## 0.55.3") < notes.index("## 0.54.0")


def test_unreleased_section_is_never_published() -> None:
    """`[Unreleased]` is not a version and must not leak into release notes."""

    notes = changelog_section.build_notes(SAMPLE, "0.55.4", "0.53.0")
    assert "Unreleased" not in notes


def test_unknown_version_is_refused() -> None:
    """Better to fail the release than publish an empty body."""

    with pytest.raises(SystemExit):
        changelog_section.build_notes(SAMPLE, "9.9.9", None)


def test_version_ordering_is_numeric_not_lexical() -> None:
    """0.55.10 is newer than 0.55.9; string comparison gets this backwards."""

    key = changelog_section._version_key
    assert key("0.55.10") > key("0.55.9")
    assert key("0.6.0") > key("0.55.4".replace("0.55", "0.5"))


def test_real_changelog_extracts_the_current_version() -> None:
    """Guards against a CHANGELOG heading format change silently breaking the
    release script."""

    root = Path(__file__).resolve().parents[2]
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    notes = changelog_section.build_notes(text, version, None)
    assert notes.strip()
