from __future__ import annotations

import uuid
from pathlib import Path

from app.core import database
from app.services import settings_store


def _open(env: Path):
    database.run_migrations(env)
    return database.connect(database.db_path(env))


def test_get_returns_none_for_unknown_key(env: Path) -> None:
    conn = _open(env)
    try:
        assert settings_store.get(conn, "missing") is None
    finally:
        conn.close()


def test_set_then_get_round_trips(env: Path) -> None:
    conn = _open(env)
    try:
        settings_store.set_(conn, "foo", "bar")
        assert settings_store.get(conn, "foo") == "bar"
    finally:
        conn.close()


def test_set_upserts_on_existing_key(env: Path) -> None:
    conn = _open(env)
    try:
        settings_store.set_(conn, "foo", "first")
        settings_store.set_(conn, "foo", "second")
        assert settings_store.get(conn, "foo") == "second"
    finally:
        conn.close()


def test_get_or_init_podcast_guid_persists_first_call(env: Path) -> None:
    conn = _open(env)
    try:
        first = settings_store.get_or_init_podcast_guid(conn, "https://feed.example.test")
        # Returned value is a valid uuid string.
        parsed = uuid.UUID(first)
        assert str(parsed) == first
        # Persisted under the documented key so feed/render can read it back.
        assert settings_store.get(conn, settings_store.PODCAST_GUID_KEY) == first
    finally:
        conn.close()


def test_get_or_init_podcast_guid_is_stable_across_calls(env: Path) -> None:
    conn = _open(env)
    try:
        a = settings_store.get_or_init_podcast_guid(conn, "https://feed.example.test")
        b = settings_store.get_or_init_podcast_guid(conn, "https://feed.example.test")
        assert a == b
    finally:
        conn.close()


def test_get_or_init_podcast_guid_is_uuid5_of_pc2_namespace(env: Path) -> None:
    """The Podcasting 2.0 spec mandates UUIDv5 over the PC2 namespace
    ``ead4c236-bf58-58c6-a2c6-a6b28d128cb6`` with the scheme stripped and
    trailing slash removed. Any other derivation produces a value no other
    PC2-aware tool will agree with."""

    conn = _open(env)
    try:
        pc2_ns = uuid.UUID("ead4c236-bf58-58c6-a2c6-a6b28d128cb6")
        derived = str(uuid.uuid5(pc2_ns, "feed.example.test"))
        stored = settings_store.get_or_init_podcast_guid(conn, "https://feed.example.test/")
        assert stored == derived
    finally:
        conn.close()


def test_canonical_feed_url_strips_scheme_and_trailing_slash() -> None:
    assert settings_store._canonical_feed_url("https://feed.example.test/") == "feed.example.test"
    assert (
        settings_store._canonical_feed_url("http://feed.example.test/path/")
        == "feed.example.test/path"
    )
    assert settings_store._canonical_feed_url("https://feed.example.test") == "feed.example.test"


def test_get_or_init_returns_persisted_value_regardless_of_base_url(
    env: Path,
) -> None:
    """Once a guid is stored, the function must NOT re-derive it even if the
    operator's BASE_URL changes (the Podcasting 2.0 spec requires the guid
    survive feed-URL changes)."""

    conn = _open(env)
    try:
        stored = settings_store.get_or_init_podcast_guid(conn, "https://old.example.test")
        unchanged = settings_store.get_or_init_podcast_guid(conn, "https://new.example.test")
        assert unchanged == stored
    finally:
        conn.close()
