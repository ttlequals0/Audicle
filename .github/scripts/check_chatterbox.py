#!/usr/bin/env python3
"""Weekly upstream check for chatterbox-tts.

Opens a GitHub issue when PyPI has a chatterbox-tts release newer than the version
pinned in ``tts-wrapper/pyproject.toml`` (the ``chatterbox-tts>=X.Y.Z`` floor). The pin
is the baseline: once the floor is bumped to the new version, the alert stops, so no
state file or bot commit is needed. The issue body carries the new release's
requires-python and key dependency pins (numpy/torch/diffusers/gradio/transformers) so
the CVE-pin and Python-version status is visible without opening PyPI.

Run by ``.github/workflows/chatterbox-monitor.yml``; ``gh`` and ``GH_TOKEN`` are provided
by the workflow. Set ``DRY_RUN=1`` to print the decision without touching issues.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

PYPI_JSON = "https://pypi.org/pypi/chatterbox-tts/json"
PYPROJECT = Path("tts-wrapper/pyproject.toml")
WATCH = ("numpy", "torch", "torchaudio", "transformers", "diffusers", "gradio")
LABEL = "chatterbox-update"


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def pinned_floor() -> str:
    m = re.search(r"chatterbox-tts>=([0-9][0-9.]*)", PYPROJECT.read_text(encoding="utf-8"))
    if not m:
        sys.exit(f"Could not find a chatterbox-tts>= pin in {PYPROJECT}")
    return m.group(1)


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


def issue_already_filed(version: str) -> bool:
    """True if an issue (open or closed) for this version already exists, so a handled
    or already-reported release is never re-filed."""

    out = subprocess.run(
        ["gh", "issue", "list", "--state", "all", "--label", LABEL,
         "--limit", "200", "--json", "title", "--jq", ".[].title"],
        capture_output=True, text=True, check=True,
    ).stdout
    return version in out


def issue_body(floor: str, state: dict) -> str:
    return "\n".join([
        "A `chatterbox-tts` release newer than the one Audicle pins is on PyPI.",
        "",
        f"- **Latest:** `{state['version']}` (the wrapper pins `chatterbox-tts>={floor}`)",
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
        "PyPI: https://pypi.org/project/chatterbox-tts/ -- Repo: https://github.com/resemble-ai/chatterbox",
        "",
        "_Filed automatically by `.github/workflows/chatterbox-monitor.yml`._",
    ])


def main() -> None:
    floor = pinned_floor()
    state = pypi_state()
    latest = state["version"]

    if version_tuple(latest) <= version_tuple(floor):
        print(f"Up to date: PyPI latest {latest} <= pinned >={floor}. Nothing to do.")
        return

    title = f"chatterbox-tts {latest} released (Audicle pins >={floor})"
    if os.environ.get("DRY_RUN") == "1":
        print(f"[dry-run] Would file issue: {title}")
        print(issue_body(floor, state))
        return

    if issue_already_filed(latest):
        print(f"Issue for chatterbox-tts {latest} already filed. Skipping.")
        return

    subprocess.run(
        ["gh", "label", "create", LABEL, "--color", "5319e7",
         "--description", "Upstream Chatterbox TTS update", "--force"],
        check=False,
    )
    subprocess.run(
        ["gh", "issue", "create", "--title", title, "--label", LABEL,
         "--body", issue_body(floor, state)],
        check=True,
    )
    print(f"Opened issue: {title}")


if __name__ == "__main__":
    main()
