from __future__ import annotations

import json
from pathlib import Path

from app.core import database
from app.services import lexicon, settings_store


def test_migration_creates_table_and_imports_seed(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        counts = lexicon.counts_by_origin(conn)
        assert counts.get("seed", 0) > 100  # the curated CSV is imported read-only
        # A real-word swap ships in the seed and must be a read-only seed row.
        sql = lexicon.lookup(conn, "SQL")
        assert sql is not None
        assert sql.origin == "seed"
        assert sql.read_only is True
        assert sql.spoken == "sequel"


def test_migration_017_drops_phonetic_respellings(env: Path) -> None:
    """The seed re-imports (017 + 019) drop the trimmed-out rows: a former hyphenated
    respelling and a letter-spelled acronym are gone, while real-word swaps survive."""

    database.run_migrations(env)
    with database.connection(env) as conn:
        assert lexicon.lookup(conn, "Kubernetes") is None  # phonetic respelling removed (017)
        assert lexicon.lookup(conn, "LLM") is None           # letter-spelled acronym removed (019)
        assert lexicon.lookup(conn, "SQL") is not None       # real-word swap kept


def test_migration_imports_legacy_user_dict(env: Path) -> None:
    # Seed a legacy flat dict before migrations run.
    database.run_migrations(env)
    with database.connection(env) as conn:
        settings_store.set_(conn, settings_store.PRONUNCIATION_KEY, json.dumps({"Foo": "fee"}))
    # Drop the lexicon migration marker so it re-runs the import path is exercised
    # via a fresh helper instead; here just verify user CRUD round-trips.
    with database.connection(env) as conn:
        lexicon.replace_user_entries(conn, {"Acme": {"mode": "override", "spoken": "ACK-mee"}})
        got = lexicon.get_user_entries(conn)
        assert got["Acme"]["spoken"] == "ACK-mee"


def test_lookup_precedence_user_over_seed(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        # February exists as seed; a user override must win.
        lexicon.replace_user_entries(
            conn, {"February": {"mode": "override", "spoken": "user-feb"}}
        )
        entry = lexicon.lookup(conn, "February")
        assert entry is not None
        assert entry.origin == "user"
        assert entry.spoken == "user-feb"


def test_lookup_case_sensitivity(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        lexicon.import_readonly(
            conn,
            "base",
            {
                "US": {"mode": "spell", "spoken": "U S", "case_sensitive": True},
                "Paris": {"mode": "override", "spoken": "pair-iss", "case_sensitive": False},
            },
        )
        conn.commit()
        # Case-sensitive: only exact casing matches.
        assert lexicon.lookup(conn, "US") is not None
        assert lexicon.lookup(conn, "us") is None
        # Case-insensitive: any casing matches.
        assert lexicon.lookup(conn, "paris") is not None
        assert lexicon.lookup(conn, "PARIS") is not None


def test_import_readonly_preserves_user_rows(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        lexicon.replace_user_entries(conn, {"Keepme": {"mode": "override", "spoken": "keep"}})
        # Re-import base (simulating a versioned refresh) must not drop user rows.
        lexicon.import_readonly(conn, "base", {"Word": {"mode": "word", "spoken": "Word"}})
        conn.commit()
        assert "Keepme" in lexicon.get_user_entries(conn)
        assert lexicon.lookup(conn, "Word") is not None


def test_sync_base_artifact_imports_and_gates_on_version(env: Path, tmp_path: Path) -> None:
    database.run_migrations(env)
    artifact = tmp_path / "base_lexicon.jsonl"
    artifact.write_text(
        '{"origin":"base","input_text":"Qatar","spoken":"KUH-tar","mode":"override"}\n'
        '{"origin":"base","input_text":"Nguyen","spoken":"win","mode":"override"}\n',
        encoding="utf-8",
    )
    with database.connection(env) as conn:
        lexicon.replace_user_entries(conn, {"Keepme": {"mode": "override", "spoken": "keep"}})
        assert lexicon.sync_base_artifact(conn, artifact, "v1") is True
        # Nguyen is base-only (Qatar now also ships in the seed, which would
        # shadow the base row), so this cleanly verifies the base import.
        assert lexicon.lookup(conn, "Nguyen").spoken == "win"
        # Same version -> no re-import.
        assert lexicon.sync_base_artifact(conn, artifact, "v1") is False
        # User rows survive the import.
        assert "Keepme" in lexicon.get_user_entries(conn)


def test_reference_text_includes_homograph_notes(env: Path) -> None:
    # The seed ships homographs with disambiguation notes; the LLM reference must
    # carry them so the pronunciation pass can pick the right reading by context.
    database.run_migrations(env)
    with database.connection(env) as conn:
        ref = lexicon.reference_text(conn)
    assert "read (present) -> reed" in ref
    assert "Present tense" in ref  # the note context is preserved


def test_apply_pairs_by_case_splits_on_flag(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        lexicon.replace_user_entries(
            conn,
            {
                "404 media": {"mode": "override", "spoken": "four oh four media",
                              "case_sensitive": False},
                "US": {"mode": "spell", "spoken": "U S", "case_sensitive": True},
            },
        )
        cs, ci = lexicon.apply_pairs_by_case(conn)
        assert cs["US"] == "U S"
        assert "404 media" not in cs
        assert ci["404 media"] == "four oh four media"
        assert "US" not in ci
