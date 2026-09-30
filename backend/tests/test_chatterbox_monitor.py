from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / ".github/scripts/check_chatterbox.py"
_SPEC = importlib.util.spec_from_file_location("check_chatterbox", _SCRIPT)
monitor = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(monitor)


def test_baseline_uses_installed_lock_version(tmp_path, monkeypatch):
    lock = tmp_path / "uv.lock"
    lock.write_text('[[package]]\nname="chatterbox-tts"\nversion="0.2.0"\n')
    monkeypatch.setattr(monitor, "LOCKFILE", lock)
    assert monitor.locked_version() == "0.2.0"


@pytest.mark.parametrize(
    "latest,expected", [("0.1.8", True), ("0.1.8rc1", False), ("0.1.7", False), ("0.1.6", False)]
)
def test_only_new_stable_pypi_versions_are_reported(monkeypatch, latest, expected):
    reports = []
    monkeypatch.setattr(monitor, "locked_version", lambda: "0.1.7")
    monkeypatch.setattr(
        monitor, "pypi_state", lambda: {"version": latest, "requires_python": ">=3.10", "pins": []}
    )
    monkeypatch.setattr(monitor, "check_repository", lambda installed: None)
    monkeypatch.setattr(monitor, "report", lambda *args: reports.append(args))
    monitor.main()
    assert bool(reports) == expected


def test_deduplication_compares_exact_titles(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, stdout=json.dumps([{"title": "chatterbox-tts 0.1.70 released"}])
        ),
    )
    assert not monitor.issue_already_filed("chatterbox-tts 0.1.7 released")
    assert monitor.issue_already_filed("chatterbox-tts 0.1.70 released")


def test_unpublished_repository_changes_are_reported(monkeypatch):
    reports = []
    head = "a" * 40

    def github(endpoint):
        if "/releases?" in endpoint:
            return [{"draft": False, "prerelease": False, "tag_name": "v0.1.2"}]
        if endpoint.endswith("/commits/HEAD"):
            return {"sha": head}
        return {
            "status": "ahead",
            "commits": [{"commit": {"message": "Add new model\nDetails"}}],
            "html_url": "https://github.com/example/compare",
        }

    monkeypatch.setattr(monitor, "github_json", github)
    monkeypatch.setattr(monitor, "report", lambda *args: reports.append(args))
    monitor.check_repository("0.1.7")
    assert len(reports) == 1
    assert reports[0][0] == f"Chatterbox source update {head[:12]}"
    assert "Add new model" in reports[0][1]


def test_dry_run_never_creates_issues(monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "1")

    def forbidden(*args, **kwargs):
        pytest.fail("Dry run must not call issue APIs")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monitor.report("Test release", "Review this version")
    assert "[dry-run] Would file issue: Test release" in capsys.readouterr().out


def test_github_release_selection_ignores_publication_order(monkeypatch):
    reports = []
    releases = [
        {
            "draft": False,
            "prerelease": False,
            "tag_name": tag,
            "html_url": "https://github.com/example/release",
        }
        for tag in ["v0.2.1", "v0.3.0", "v0.4.0rc1"]
    ]
    monkeypatch.setattr(
        monitor,
        "github_json",
        lambda endpoint: releases if "/releases?" in endpoint else {"sha": monitor.SOURCE_BASELINE},
    )
    monkeypatch.setattr(monitor, "report", lambda *args: reports.append(args))
    monitor.check_repository("0.1.7")
    assert len(reports) == 1
    assert reports[0][0] == "chatterbox-tts 0.3.0 released"
