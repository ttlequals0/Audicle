#!/usr/bin/env python3
"""Monitor PyPI releases, GitHub releases, and unpublished Chatterbox source changes.

Set DRY_RUN=1 to print findings without changing GitHub issues.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import tomllib
import urllib.request
from pathlib import Path

from packaging.version import InvalidVersion, Version

PYPI_JSON = "https://pypi.org/pypi/chatterbox-tts/json"
LOCKFILE = Path("tts-wrapper/uv.lock")
# Source of the installed 0.1.7 release; advance after reviewing/adopting upstream changes.
SOURCE_BASELINE = "59bc590b3cad826e5d5987745bf6844627a21ad5"
UPSTREAM = "resemble-ai/chatterbox"
WATCH = ("numpy", "torch", "torchaudio", "transformers", "diffusers", "gradio")
LABEL = "chatterbox-update"


def locked_version() -> str:
    with LOCKFILE.open("rb") as source:
        packages = tomllib.load(source)["package"]
    return next(package["version"] for package in packages if package["name"] == "chatterbox-tts")


def pypi_state() -> dict:
    with urllib.request.urlopen(PYPI_JSON, timeout=30) as resp:
        info = json.load(resp)["info"]
    watch_re = re.compile(rf"^({'|'.join(WATCH)})\b", re.IGNORECASE)
    pins = [d for d in (info.get("requires_dist") or []) if watch_re.match(d)]
    return {
        "version": info["version"],
        "requires_python": info.get("requires_python"),
        "pins": pins,
    }


def issue_already_filed(title: str) -> bool:
    """True if an issue (open or closed) for this version already exists, so a handled
    or already-reported release is never re-filed."""

    out = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--state",
            "all",
            "--label",
            LABEL,
            "--limit",
            "1000",
            "--json",
            "title",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return any(issue["title"] == title for issue in json.loads(out))


def issue_body(floor: str, state: dict) -> str:
    return "\n".join(
        [
            "A `chatterbox-tts` release newer than the one Audicle pins is on PyPI.",
            "",
            f"- **Latest:** `{state['version']}` (the wrapper lockfile contains `{floor}`)",
            f"- **requires-python:** `{state['requires_python']}`",
            "",
            "Key dependency pins in this release:",
            "```",
            *state["pins"],
            "```",
            "",
            "Before pulling it into `tts-wrapper/`:",
            "- [ ] Did `diffusers` / `gradio` move off their exact pins (the HIGH-CVE transitive pins)?",
            "- [ ] Did `numpy` / `requires-python` change the supported Python range (3.13 / 3.14)?",
            "- [ ] Any new `ChatterboxTurboTTS` / `generate()` behavior or text/pronunciation features?",
            "- [ ] Rebuild the GPU wrapper image, run the TTS smoke test, then bump the pin in",
            "      `tts-wrapper/pyproject.toml` and relock. (Closing this issue stops the reminder.)",
            "",
            "PyPI: https://pypi.org/project/chatterbox-tts/",
            "Repo: https://github.com/resemble-ai/chatterbox",
            "",
            "_Filed automatically by `.github/workflows/chatterbox-monitor.yml`._",
        ]
    )


def report(title: str, body: str) -> None:
    if os.environ.get("DRY_RUN") == "1":
        print(f"[dry-run] Would file issue: {title}")
        print(body)
        return

    if issue_already_filed(title):
        print(f"Already reported: {title}")
        return

    subprocess.run(
        [
            "gh",
            "label",
            "create",
            LABEL,
            "--color",
            "5319e7",
            "--description",
            "Upstream Chatterbox TTS update",
            "--force",
        ],
        check=True,
    )
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".md", dir="."
    ) as body_file:
        body_file.write(body)
        body_file.flush()
        subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--title",
                title,
                "--label",
                LABEL,
                "--body-file",
                body_file.name,
            ],
            check=True,
        )
    print(f"Opened issue: {title}")


def github_json(endpoint: str):
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def check_repository(installed: str) -> None:
    releases = github_json(f"repos/{UPSTREAM}/releases?per_page=100")
    newest = None
    newest_version = Version(installed)
    for release in releases:
        if release["draft"] or release["prerelease"]:
            continue
        try:
            version = Version(release["tag_name"])
        except InvalidVersion:
            continue
        if not version.is_prerelease and not version.is_devrelease and version > newest_version:
            newest, newest_version = release, version
    if newest:
        report(
            f"chatterbox-tts {newest_version} released",
            f"Upstream published a release: {newest['html_url']}\n\n"
            f"Audicle locks chatterbox-tts {installed}. Review compatibility before updating.\n",
        )
    head = github_json(f"repos/{UPSTREAM}/commits/HEAD")
    if head["sha"] == SOURCE_BASELINE:
        return
    comparison = github_json(f"repos/{UPSTREAM}/compare/{SOURCE_BASELINE}...{head['sha']}")
    if comparison["status"] not in {"ahead", "diverged"}:
        return
    changes = [commit["commit"]["message"].splitlines()[0] for commit in comparison["commits"]]
    report(
        f"Chatterbox source update {head['sha'][:12]}",
        "The upstream repository has changes beyond the reviewed source baseline. "
        "These may not be published to PyPI yet.\n\n"
        + "\n".join(f"- {change}" for change in changes)
        + f"\n\nCompare: {comparison['html_url']}\n\n"
        "Review synthesis and dependency changes before adopting them. Advance "
        "`SOURCE_BASELINE` in `.github/scripts/check_chatterbox.py` after review.\n",
    )


def main() -> None:
    installed = locked_version()
    state = pypi_state()
    latest = Version(state["version"])
    if not latest.is_prerelease and not latest.is_devrelease and latest > Version(installed):
        report(f"chatterbox-tts {latest} released", issue_body(installed, state))
    else:
        print(f"PyPI: {latest}; installed lockfile: {installed}. No newer stable release.")
    check_repository(installed)


if __name__ == "__main__":
    main()
