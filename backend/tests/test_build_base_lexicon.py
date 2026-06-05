from __future__ import annotations

import importlib.util
from pathlib import Path

from app.core import database
from app.services import lexicon

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "build_base_lexicon.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("build_base_lexicon", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_arpabet_to_ipa() -> None:
    mod = _load_module()
    assert mod._arpabet_to_ipa("K AE1 T") == "kæt"
    assert mod._arpabet_to_ipa("HH AH0 L OW1") == "hʌloʊ"


def test_build_journalism_artifact_imports(env: Path, tmp_path: Path) -> None:
    mod = _load_module()
    out = tmp_path / "base_lexicon.jsonl"
    counts = mod.build(out, only=["journalism"], limit=None, cache=tmp_path / "cache")
    assert counts["journalism"] == len(mod._JOURNALISM)
    assert out.exists()
    # The artifact imports cleanly into the read-only base rows.
    database.run_migrations(env)
    with database.connection(env) as conn:
        assert lexicon.sync_base_artifact(conn, out, "vtest") is True
        # BRICS is not in the seed, so the base row is what lookup returns.
        brics = lexicon.lookup(conn, "BRICS")
        assert brics is not None
        assert brics.origin == "base"
        assert brics.mode == "word"
        assert brics.spoken == "BRICS"
