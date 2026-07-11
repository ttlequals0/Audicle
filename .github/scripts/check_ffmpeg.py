#!/usr/bin/env python3
"""Weekly upstream check for the pinned static ffmpeg binary.

Opens a GitHub issue when BtbN/FFmpeg-Builds has a newer stable linux64-gpl
major series than the one baked into the ADD URL in the root Dockerfile.

Background: BtbN publishes two kinds of builds:
  - Daily autobuilds (e.g. ffmpeg-n8.1.2-22-g94138f6973-linux64-gpl-8.1.tar.xz)
    that are deleted after ~14 days. Audicle mirrors these to a repo release so
    the SHA-verified URL stays alive.
  - Stable branch snapshots (e.g. ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz)
    that stay in the BtbN releases page permanently.

The pinned token in the Dockerfile is a daily-autobuild tag (nMAJOR.MINOR.PATCH-N-gHASH).
We extract the major series (nMAJOR.MINOR) and compare it against the highest
n<MAJOR>.<MINOR>-latest-linux64-gpl-<MAJOR>.<MINOR>.tar.xz series on BtbN's
latest release. If BtbN has a newer series (e.g. n8.2 when we pin n8.1), we flag it.

Run by .github/workflows/ffmpeg-monitor.yml; gh and GH_TOKEN are provided by
the workflow. Without GH_TOKEN the script still compares versions and prints
what it would do (useful for local dry-run verification).

Pass --dry-run (or set DRY_RUN=1) to print the comparison without touching issues.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

DOCKERFILE = Path("Dockerfile")
BTBN_RELEASES_API = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
# Match the pinned version token in Dockerfile ADD URL (daily autobuild format)
# e.g. ffmpeg-n8.1.2-22-g94138f6973-linux64-gpl
DOCKERFILE_VER_RE = re.compile(
    r"ffmpeg-(n([0-9]+\.[0-9]+)\.[0-9.]+(?:-[0-9]+-g[0-9a-f]+)?)-linux64-gpl"
)
# Match BtbN stable assets: ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz
BTBN_STABLE_RE = re.compile(
    r"^ffmpeg-(n([0-9]+\.[0-9]+))-latest-linux64-gpl-[0-9]+\.[0-9]+\.tar\.xz$"
)
LABEL = "ffmpeg-update"


def pinned_series() -> tuple[str, str]:
    """Return (full_token, major_series) from the Dockerfile ADD URL.

    full_token: e.g. n8.1.2-22-g94138f6973
    major_series: e.g. n8.1
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    m = DOCKERFILE_VER_RE.search(text)
    if not m:
        sys.exit(f"Could not find a pinned ffmpeg version token in {DOCKERFILE}")
    return m.group(1), "n" + m.group(2)


def latest_btbn_series() -> str:
    """Return the highest nMAJOR.MINOR series available as a stable linux64-gpl asset."""
    req = urllib.request.Request(
        BTBN_RELEASES_API,
        headers={"Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
    )
    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if gh_token:
        req.add_header("Authorization", f"Bearer {gh_token}")

    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)

    assets = [a["name"] for a in data.get("assets", [])]
    matches = [BTBN_STABLE_RE.match(a) for a in assets]
    series = [m.group(2) for m in matches if m]  # e.g. ["8.1", "7.1"]
    if not series:
        sys.exit(
            "No stable linux64-gpl asset found in the latest BtbN release.\n"
            f"Assets seen: {assets}"
        )

    def ver_key(s: str) -> tuple[int, ...]:
        return tuple(int(x) for x in s.split("."))

    best = max(series, key=ver_key)
    return "n" + best


def issue_already_filed(latest_series: str) -> bool:
    out = subprocess.run(
        ["gh", "issue", "list", "--state", "all", "--label", LABEL,
         "--limit", "200", "--json", "title", "--jq", ".[].title"],
        capture_output=True, text=True, check=True,
    ).stdout
    return latest_series in out


def issue_body(pinned_token: str, pinned_series: str, latest_series: str) -> str:
    return "\n".join([
        "The static ffmpeg binary baked into the Audicle app image is behind the",
        "latest stable BtbN/FFmpeg-Builds linux64-gpl major series.",
        "",
        f"- **Pinned series:** `{pinned_series}` (token `{pinned_token}` in root Dockerfile)",
        f"- **Latest BtbN series:** `{latest_series}-latest` (stable branch snapshot)",
        "",
        "ffmpeg parses untrusted audio uploads; staying current is a security concern.",
        "The binary is static and invisible to trivy (no package DB entry), so this",
        "workflow is the only update signal.",
        "",
        "To bump:",
        "1. Download the new `ffmpeg-" + latest_series + "-latest-linux64-gpl-*.tar.xz`",
        "   from https://github.com/BtbN/FFmpeg-Builds/releases",
        "   (or grab a daily autobuild asset for a specific commit if preferred).",
        "2. Mirror the tarball to a new Audicle release tag (BtbN daily autobuilds",
        "   are deleted after ~14 days; stable branch snapshots stay).",
        "3. Update the ADD URL and `--checksum=sha256:` in the root Dockerfile.",
        "4. Rebuild and re-run `scripts/trivy_gate.sh`.",
        "5. Close this issue once the Dockerfile is updated.",
        "",
        "BtbN releases: https://github.com/BtbN/FFmpeg-Builds/releases",
        "",
        "_Filed automatically by `.github/workflows/ffmpeg-monitor.yml`._",
    ])


def main() -> None:
    dry_run = "--dry-run" in sys.argv or os.environ.get("DRY_RUN") == "1"

    pinned_token, p_series = pinned_series()
    print(f"Pinned ffmpeg token  : {pinned_token}  (series {p_series})")

    try:
        l_series = latest_btbn_series()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not fetch BtbN latest release: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Latest BtbN series   : {l_series}")

    def ver_key(s: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", s))

    if ver_key(l_series) <= ver_key(p_series):
        print("Up to date. Nothing to do.")
        return

    title = f"Pinned static ffmpeg is behind upstream ({p_series} -> {l_series})"
    print(f"Out of date: {title}")

    if dry_run:
        print("[dry-run] Would file issue:")
        print(f"  Title: {title}")
        print("  Body:")
        for line in issue_body(pinned_token, p_series, l_series).splitlines():
            print(f"    {line}")
        return

    if not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        print("No GH_TOKEN set; skipping issue creation. Re-run in CI or with GH_TOKEN.")
        return

    if issue_already_filed(l_series):
        print(f"Issue for {l_series} already filed. Skipping.")
        return

    subprocess.run(
        ["gh", "label", "create", LABEL, "--color", "e4e669",
         "--description", "Static ffmpeg upstream update available", "--force"],
        check=False,
    )
    subprocess.run(
        ["gh", "issue", "create", "--title", title, "--label", LABEL,
         "--body", issue_body(pinned_token, p_series, l_series)],
        check=True,
    )
    print(f"Opened issue: {title}")


if __name__ == "__main__":
    main()
