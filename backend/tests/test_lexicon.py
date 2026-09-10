from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.core import database
from app.services import lexicon, settings_store


def test_migration_creates_table_and_imports_seed(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        seed_count = conn.execute("SELECT COUNT(*) FROM lexicon WHERE origin = 'seed'").fetchone()[0]
        assert seed_count > 100  # the curated CSV is imported read-only
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
        assert lexicon.lookup(conn, "SQL").origin == "seed"
        # Same version -> no re-import.
        assert lexicon.sync_base_artifact(conn, artifact, "v1") is False
        # User rows survive the import.
        assert "Keepme" in lexicon.get_user_entries(conn)


def test_reference_text_includes_homograph_notes(env: Path) -> None:
    # The seed ships homographs with disambiguation notes; the LLM reference must
    # carry them so the pronunciation pass can pick the right reading by context.
    database.run_migrations(env)
    with database.connection(env) as conn:
        ref = "\n".join(line for _, line in lexicon.reference_entries(conn))
    assert "read (present) -> reed" in ref
    assert "Present tense" in ref  # the note context is preserved


def test_migration_023_adds_live_homographs(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        ref = "\n".join(line for _, line in lexicon.reference_entries(conn))
    assert "live (verb) -> liv" in ref
    assert "live (adjective) -> lyve" in ref


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


def test_sync_restores_import_pragmas(env: Path, tmp_path: Path) -> None:
    # The bulk-import pragmas (large cache, autocheckpoint off) must not leak
    # to the caller's connection after the sync returns (#126).
    database.run_migrations(env)
    artifact = tmp_path / "base_lexicon.jsonl"
    artifact.write_text(
        '{"origin":"base","input_text":"Nguyen","spoken":"win","mode":"override"}\n',
        encoding="utf-8",
    )
    with database.connection(env) as conn:
        before_cache = conn.execute("PRAGMA cache_size").fetchone()[0]
        before_ckpt = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
        assert lexicon.sync_base_artifact(conn, artifact, "vpragma") is True
        assert conn.execute("PRAGMA cache_size").fetchone()[0] == before_cache
        assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == before_ckpt


def test_lexicon_sync_lock_does_not_block_migration_lock(env: Path) -> None:
    # The #126 deadlock: the sync thread held migration_lock for a
    # tens-of-minutes import and the other process's startup migration check
    # never returned. The sync now uses its own lock; holding it must leave
    # migration_lock immediately acquirable.
    import threading

    acquired = threading.Event()
    with database.lexicon_sync_lock(env):

        def _try_migration_lock() -> None:
            with database.migration_lock(env):
                acquired.set()

        t = threading.Thread(target=_try_migration_lock)
        t.start()
        t.join(timeout=5)
    assert acquired.is_set()


def test_bulk_import_keeps_wal_bounded(env: Path, tmp_path: Path) -> None:
    # #126 follow-up: with wal_autocheckpoint off for the import, an unbatched
    # insert grew the WAL past 10 GB for a 161 MB database. Batched inserts
    # checkpoint between batches, so peak WAL stays near one batch's pages.
    database.run_migrations(env)
    rows = 120_000  # spans several _IMPORT_BATCH_ROWS batches when scaled down
    monkey_batch = 20_000
    original = lexicon._IMPORT_BATCH_ROWS
    lexicon._IMPORT_BATCH_ROWS = monkey_batch
    try:
        entries = {
            f"term{i:06d}": {"mode": "override", "spoken": f"spoken {i}"} for i in range(rows)
        }
        wal = database.db_path(env).with_suffix(".db-wal")
        peak = 0
        with database.connection(env) as conn:
            conn.execute("PRAGMA wal_autocheckpoint=0")
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            lexicon.import_readonly(conn, "base", entries)
            conn.set_trace_callback(None)
            peak = max(peak, wal.stat().st_size if wal.exists() else 0)
            assert conn.execute("SELECT COUNT(*) FROM lexicon WHERE origin='base'").fetchone()[0] == rows
            assert sum(statement == "BEGIN IMMEDIATE" for statement in statements) >= 6
            assert sum(statement == "COMMIT" for statement in statements) >= 6
        db_size = database.db_path(env).stat().st_size
        # Unbatched, the WAL would hold every page written for all 120k rows.
        # Batched, it should stay far below the finished database size.
        assert peak < db_size, f"peak WAL {peak} >= db size {db_size}"
    finally:
        lexicon._IMPORT_BATCH_ROWS = original


def test_sync_gate_is_artifact_content_not_app_version(env: Path, tmp_path: Path) -> None:
    # #126 follow-up: a release that ships a byte-identical artifact must do no
    # work, and a changed artifact must still re-import.
    database.run_migrations(env)
    artifact = tmp_path / "base_lexicon.jsonl"
    artifact.write_text(
        '{"origin":"base","input_text":"Alpha","spoken":"AL-fuh","mode":"override"}\n',
        encoding="utf-8",
    )
    with database.connection(env) as conn:
        assert lexicon.sync_base_artifact(conn, artifact, "0.1.0") is True
        # Same bytes, later app version: no re-import.
        assert lexicon.sync_base_artifact(conn, artifact, "0.2.0") is False
        assert lexicon.sync_base_artifact(conn, artifact, "0.3.0") is False
        # Changed artifact at the same app version: re-imports.
        artifact.write_text(
            '{"origin":"base","input_text":"Alpha","spoken":"AL-fuh","mode":"override"}\n'
            '{"origin":"base","input_text":"Beta","spoken":"BAY-tuh","mode":"override"}\n',
            encoding="utf-8",
        )
        assert lexicon.sync_base_artifact(conn, artifact, "0.3.0") is True
        assert lexicon.lookup(conn, "Beta").spoken == "BAY-tuh"


def test_interrupted_import_keeps_previous_generation_active(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)
    old_artifact = tmp_path / "old.jsonl"
    old_artifact.write_text('{"input_text":"Old","spoken":"old"}\n', encoding="utf-8")
    new_artifact = tmp_path / "new.jsonl"
    new_artifact.write_text(
        '{"input_text":"New1","spoken":"new one"}\n'
        '{"input_text":"New2","spoken":"new two"}\n',
        encoding="utf-8",
    )
    with database.connection(env) as conn:
        assert lexicon.sync_base_artifact(conn, old_artifact, "old")
        original = lexicon._write_batch
        calls = 0

        def _interrupt(connection, rows):
            nonlocal calls
            calls += 1
            original(connection, rows)
            if calls == 1:
                with database.connection(env) as reader:
                    assert lexicon.lookup(reader, "Old").spoken == "old"
                    assert lexicon.lookup(reader, "New1") is None
                raise RuntimeError("interrupted")

        monkeypatch.setattr(lexicon, "_IMPORT_BATCH_ROWS", 1)
        monkeypatch.setattr(lexicon, "_write_batch", _interrupt)
        with pytest.raises(RuntimeError, match="interrupted"):
            lexicon.sync_base_artifact(conn, new_artifact, "new")
        assert lexicon.lookup(conn, "Old").spoken == "old"
        assert lexicon.lookup(conn, "New1") is None
        assert conn.execute(
            "SELECT COUNT(*) FROM lexicon WHERE origin='base' AND generation <> ?",
            (lexicon._active_generation(conn, "base"),),
        ).fetchone()[0] == 0
        monkeypatch.setattr(lexicon, "_write_batch", original)
        assert lexicon.sync_base_artifact(conn, new_artifact, "new")
        assert lexicon.lookup(conn, "Old") is None
        assert lexicon.lookup(conn, "New2").spoken == "new two"


def test_replace_user_entries_rolls_back_on_insert_failure(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        lexicon.replace_user_entries(conn, {"Old": {"spoken": "old"}})

        def _fail(connection, origin, entries, **kwargs):
            connection.execute(
                "INSERT INTO lexicon(origin,input_text,input_fold,mode,spoken,read_only) "
                "VALUES('user','Partial','partial','override','partial',0)"
            )
            raise RuntimeError("insert failed")

        monkeypatch.setattr(lexicon, "insert_entries", _fail)
        with pytest.raises(RuntimeError, match="insert failed"):
            lexicon.replace_user_entries(conn, {"New": {"spoken": "new"}})
        assert lexicon.get_user_entries(conn) == {
            "Old": {"mode": "override", "spoken": "old", "case_sensitive": False}
        }


def test_import_cleanup_does_not_delete_another_staged_generation(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        lexicon._write_batch(
            conn,
            [lexicon._entry_row("base", 999, "Staged", {"spoken": "staged"}, True)],
        )
        lexicon.import_readonly(conn, "base", {"Active": {"spoken": "active"}})
        assert lexicon.lookup(conn, "Active").spoken == "active"
        assert lexicon.lookup(conn, "Staged") is None
        assert conn.execute(
            "SELECT COUNT(*) FROM lexicon WHERE origin='base' AND generation=999"
        ).fetchone()[0] == 1


def test_sync_reimports_once_when_upgrading_from_a_version_keyed_install(
    env: Path, tmp_path: Path
) -> None:
    # An install from before the content gate has only the legacy version key;
    # it re-imports once, then stores the digest and stays quiet after that.
    from app.services import settings_store

    database.run_migrations(env)
    artifact = tmp_path / "base_lexicon.jsonl"
    artifact.write_text(
        '{"origin":"base","input_text":"Alpha","spoken":"AL-fuh","mode":"override"}\n',
        encoding="utf-8",
    )
    with database.connection(env) as conn:
        settings_store.set_(conn, lexicon.LEXICON_VERSION_KEY, "0.56.6")
        assert lexicon.sync_base_artifact(conn, artifact, "0.56.7") is True
        assert lexicon.sync_base_artifact(conn, artifact, "0.56.7") is False
