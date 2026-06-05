#!/usr/bin/env python3
"""One-time re-tune of the seed pronunciation list for Chatterbox.

The bundled respellings in ``tts_correction_list.csv`` encode stress as ALL-CAPS
syllables (``FEB-roo-air-ee``, ``LIN-uks``, ``koo-BER-neh-tees``) -- a convention
tuned for XTTS-v2. Chatterbox reads a run of 2+ capital letters as letters to
spell out ("F-E-B"), which breaks the word. This script lowercases every run of
2+ consecutive uppercase letters in the ``replacement_text`` column while leaving
single capital letters alone, so letter-spelling rows (``A P I``, ``C E O``) are
preserved.

Idempotent: running twice is a no-op. Pass --check to fail (exit 1) if the file
is not already transformed (for CI), or no args to rewrite it in place.

    uv run python scripts/retune_respellings.py
    uv run python scripts/retune_respellings.py --check
"""

from __future__ import annotations

import csv
import io
import re
import sys
from pathlib import Path

_CSV = Path(__file__).resolve().parent.parent / "backend" / "app" / "defaults" / "tts_correction_list.csv"
_CAPS_RUN = re.compile(r"[A-Z]{2,}")
_FIELDS = ("category", "input_text", "replacement_text", "notes")


def lower_caps_runs(text: str) -> str:
    """Lowercase each run of 2+ consecutive uppercase letters; leave single
    capitals (letter-spelling) untouched."""

    return _CAPS_RUN.sub(lambda m: m.group(0).lower(), text)


def _transform_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    changed = 0
    for row in rows:
        old = row["replacement_text"]
        new = lower_caps_runs(old)
        if new != old:
            row["replacement_text"] = new
            changed += 1
    return rows, changed


def _render(rows: list[dict[str, str]]) -> str:
    # Preserve the file's CRLF endings so the diff is only the changed cells.
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_FIELDS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    with _CSV.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    rows, changed = _transform_rows(rows)
    if check_only:
        if changed:
            print(f"{changed} row(s) still have ALL-CAPS respellings; run without --check.")
            return 1
        print("Respellings already lowercased.")
        return 0
    _CSV.write_bytes(_render(rows).encode("utf-8"))
    print(f"Lowercased ALL-CAPS respelling runs in {changed} row(s); wrote {_CSV}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
