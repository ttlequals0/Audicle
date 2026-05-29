"""Regenerate ``openapi.yaml`` from the live FastAPI app.

Usage:
    uv run python scripts/dump_openapi.py

(The script inserts ``backend`` onto ``sys.path`` itself, so no PYTHONPATH is
needed.)
"""

from __future__ import annotations

import json

# A few env vars are required by Settings; supply minimal placeholders so the
# script can run outside a deployed environment.
import os
import sys
from pathlib import Path

import yaml

os.environ.setdefault("BASE_URL", "https://example.test")
os.environ.setdefault("UI_BASE_URL", "https://example.test")
os.environ.setdefault("DATA_DIR", "/tmp/audicle-openapi")
os.environ.setdefault("FIRECRAWL_URL", "http://localhost:3002")
os.environ.setdefault("TTS_URL", "http://localhost:8001")
os.environ.setdefault("LLM_PROVIDER", "openai-compatible")
os.environ.setdefault("LLM_MODEL", "placeholder")
os.environ.setdefault("OPENAI_BASE_URL", "http://localhost:11434/v1")
os.environ.setdefault("OPENAI_API_KEY", "placeholder")
os.environ.setdefault("FEED_TITLE", "Example")
os.environ.setdefault("FEED_DESCRIPTION", "Example")
os.environ.setdefault("FEED_AUTHOR", "Example")
os.environ.setdefault("FEED_EMAIL", "example@example.test")
os.environ.setdefault("FEED_ARTWORK_URL", "https://example.test/art.jpg")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.main import create_app  # noqa: E402


def main() -> int:
    app = create_app()
    schema = app.openapi()
    out = ROOT / "openapi.yaml"
    out.write_text(yaml.safe_dump(schema, sort_keys=False), encoding="utf-8")
    print(f"wrote {out} ({len(json.dumps(schema)):,} bytes of schema)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
