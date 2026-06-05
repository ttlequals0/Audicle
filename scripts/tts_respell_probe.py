#!/usr/bin/env python3
"""Probe Chatterbox pronunciation of respelling variants (Phase 0, run by hand).

Synthesizes a fixed set of words in several respelling styles so you can listen
and confirm which the model reads correctly. This is what validates the
"ALL-CAPS stress is read as letters" hypothesis behind the 0.19.0 respelling
re-tune: if "FEB-roo-air-ee" reads "F-E-B roo air ee" but "feb-roo-air-ee" reads
"February", the lowercase rule is correct.

It POSTs each variant to the wrapper's /generate endpoint (one episode_id per
variant). /generate writes a WAV into the shared /data/media volume and returns
its path; pass --media-dir (the host-visible path of that volume) to have the
script copy each WAV out to --out-dir named by its label, ready to play.

    # wrapper reachable at localhost:8000, /data/media bind-mounted at ./data/media
    python scripts/tts_respell_probe.py \
        --tts-url http://localhost:8000 \
        --media-dir ./data/media \
        --out-dir ~/Downloads/respell_probe

A/B the temperature+seed change: run once against the wrapper on the OLD env
(CHATTERBOX_TEMPERATURE=0.8, CHATTERBOX_SEED=0), restart the wrapper with the
NEW defaults (0.5 / 1234), run again into a different --out-dir, and compare.
"""

from __future__ import annotations

import argparse
import json
import shutil
import urllib.request
from pathlib import Path

# (label, text) -- each text is one phrase synthesized on its own. Variants of a
# word are grouped so the output files sort together for easy comparison.
VARIANTS: list[tuple[str, str]] = [
    ("february_plain", "It happened in February."),
    ("february_caps", "It happened in FEB-roo-air-ee."),
    ("february_lower", "It happened in feb-roo-air-ee."),
    ("february_spaced", "It happened in feb roo air ee."),
    ("january_plain", "Back in January."),
    ("january_caps", "Back in JAN-yoo-air-ee."),
    ("january_lower", "Back in jan-yoo-air-ee."),
    ("linux_plain", "We run Linux."),
    ("linux_caps", "We run LIN-uks."),
    ("linux_lower", "We run lin-uks."),
    ("linux_alt", "We run linnucks."),
    ("cuda_plain", "It uses CUDA."),
    ("cuda_lower", "It uses koo-duh."),
    ("cuda_spaced", "It uses koo duh."),
    ("os_plain", "The OS crashed."),
    ("os_lower", "The oh ess crashed."),
    ("vms_plain", "Three VMs failed."),
    ("vms_lower", "Three vee emz failed."),
    # Letter-spelling controls: single capitals must still read as letter names.
    ("api_spaced", "Call the A P I."),
    ("ieee_word", "Published by I triple E."),
]


def _post_generate(tts_url: str, text: str, episode_id: str) -> dict:
    payload = json.dumps(
        {"text": text, "episode_id": episode_id, "chunk_index": 0}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{tts_url.rstrip('/')}/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:  # operator-supplied URL
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tts-url", default="http://localhost:8000")
    parser.add_argument(
        "--media-dir",
        default=None,
        help="Host-visible path of the wrapper's /data/media; if set, WAVs are copied out.",
    )
    parser.add_argument("--out-dir", default="~/Downloads/respell_probe")
    args = parser.parse_args()

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    media_dir = Path(args.media_dir).expanduser() if args.media_dir else None

    for label, text in VARIANTS:
        episode_id = f"probe_{label}"
        try:
            result = _post_generate(args.tts_url, text, episode_id)
        except Exception as exc:  # report the failure and continue the sweep
            print(f"[FAIL] {label}: {exc}")
            continue
        wav_path = result.get("wav_path", "")
        print(f"[ok]   {label}: {result.get('duration_secs')}s -> {wav_path}")
        if media_dir and wav_path:
            src = media_dir / Path(wav_path).name
            if src.exists():
                shutil.copyfile(src, out_dir / f"{label}.wav")
            else:
                print(f"       (could not find {src} to copy; check --media-dir)")

    print(f"\nDone. If --media-dir was set, listen to the WAVs in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
