"""Authenticated-feeds tests: the pure helpers, the enforcement guard on the
public feed/media surface, URL keying in the rendered RSS, and the management
endpoints."""

from __future__ import annotations

from pathlib import Path

import defusedxml.ElementTree as DET
from app.config import get_settings
from app.core import database
from app.core.paths import media_dir
from app.main import create_app
from app.services import auth, episodes, feed_auth, runtime_settings
from fastapi.testclient import TestClient

_KEY = "a" * 64  # a valid 64-hex key
_KEY2 = "b" * 64
_PODCAST_NS = "https://podcastindex.org/namespace/1.0"


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def _enable(env: Path, *, key: str = _KEY) -> None:
    """Seed the runtime-settings overlay so feed auth is on with ``key``."""

    conn = database.connect(database.db_path(env))
    try:
        runtime_settings.set_value(conn, "FEED_AUTH_KEY", key)
        runtime_settings.set_value(conn, "FEED_AUTH_ENABLED", True)
    finally:
        conn.close()


def _seed_episode(env: Path) -> Path:
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id="ep",
            job_id=None,
            original_url="https://example.test/a",
            title="An Article",
            author="Author",
            audio_path="/data/media/ep.mp3",
            artwork_path="/data/media/ep.jpg",
            transcript_vtt="WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.000\nhi\n",
            duration_secs=10,
        )
        conn.execute("UPDATE episodes SET pub_date='2026-05-28T18:00:00Z' WHERE id='ep'")
        conn.commit()
    finally:
        conn.close()
    media = media_dir(get_settings())
    media.mkdir(parents=True, exist_ok=True)
    (media / "ep.mp3").write_bytes(b"ID3fake")
    (media / "ep.jpg").write_bytes(b"\xff\xd8\xff")
    return media


# --- pure helpers ----------------------------------------------------------


def test_verify_matrix() -> None:
    assert feed_auth.verify(_KEY, _KEY) is True
    assert feed_auth.verify(_KEY2, _KEY) is False
    assert feed_auth.verify(None, _KEY) is False
    assert feed_auth.verify("", _KEY) is False
    assert feed_auth.verify(_KEY, None) is False
    # Too short / not 64-hex never matches.
    assert feed_auth.verify("abc", _KEY) is False
    # Non-ASCII must return False, not raise (compare_digest TypeError -> 500).
    assert feed_auth.verify("é" * 64, _KEY) is False


def test_split_cover_token() -> None:
    assert feed_auth.split_cover_token(f"ep-{_KEY}") == ("ep", _KEY)
    assert feed_auth.split_cover_token("ep") == ("ep", None)
    assert feed_auth.split_cover_token("ep-notakey") == ("ep-notakey", None)
    assert feed_auth.split_cover_token(None) == (None, None)


def test_active_key_gates_on_enabled() -> None:
    base = get_settings()
    off = base.model_copy(update={"FEED_AUTH_ENABLED": False, "FEED_AUTH_KEY": _KEY})
    on = base.model_copy(update={"FEED_AUTH_ENABLED": True, "FEED_AUTH_KEY": _KEY})
    on_nokey = base.model_copy(update={"FEED_AUTH_ENABLED": True, "FEED_AUTH_KEY": ""})
    assert feed_auth.active_key(off) is None  # retained key, but disabled
    assert feed_auth.active_key(on) == _KEY
    assert feed_auth.active_key(on_nokey) is None


# --- enforcement (401 matrix) ---------------------------------------------


def test_disabled_feed_serves_without_key(env: Path) -> None:
    _seed_episode(env)
    with _client(env) as client:
        assert client.get("/rss/test_feed.xml").status_code == 200
        assert client.get("/media/ep.mp3").status_code == 200
        assert client.get("/media/ep.jpg").status_code == 200
        assert client.get("/media/ep.vtt").status_code == 200


def test_authenticated_admin_reads_media_without_key(env: Path) -> None:
    # The logged-in operator's dashboard links to /media/... relatively; the session
    # cookie exempts them from the feed key. Public clients (no session) still need it.
    _seed_episode(env)
    _enable(env)
    conn = database.connect(database.db_path(env))
    try:
        auth.set_password(conn, "s3cret-pass")
    finally:
        conn.close()
    with _client(env) as client:
        assert client.get("/media/ep.vtt").status_code == 401  # no session, no key
        assert client.post("/api/v1/auth/login", json={"password": "s3cret-pass"}).status_code == 200
        # Same keyless requests now succeed via the session cookie.
        assert client.get("/media/ep.mp3").status_code == 200
        assert client.get("/media/ep.jpg").status_code == 200
        assert client.get("/media/ep.vtt").status_code == 200


def test_enabled_feed_rejects_missing_and_wrong_key(env: Path) -> None:
    _seed_episode(env)
    _enable(env)
    with _client(env) as client:
        assert client.get("/rss/test_feed.xml").status_code == 401
        assert client.get(f"/rss/test_feed.xml?key={_KEY2}").status_code == 401
        assert client.get("/media/ep.mp3").status_code == 401
        assert client.get("/media/ep.vtt").status_code == 401
        # HEAD is gated too (served through the GET view).
        assert client.head("/rss/test_feed.xml").status_code == 401


def test_enabled_feed_accepts_correct_key(env: Path) -> None:
    _seed_episode(env)
    _enable(env)
    with _client(env) as client:
        assert client.get(f"/rss/test_feed.xml?key={_KEY}").status_code == 200
        assert client.get(f"/media/ep.mp3?key={_KEY}").status_code == 200
        assert client.get(f"/media/ep.vtt?key={_KEY}").status_code == 200
        # Cover key rides the path token, not a query string.
        assert client.get(f"/media/ep-{_KEY}.jpg").status_code == 200
        # A wrong key in the cover token 401s; a keyless cover 401s too.
        assert client.get(f"/media/ep-{_KEY2}.jpg").status_code == 401
        assert client.get("/media/ep.jpg").status_code == 401


# --- URL keying in the rendered feed --------------------------------------


def test_rendered_urls_carry_the_key(env: Path) -> None:
    _seed_episode(env)
    _enable(env)
    with _client(env) as client:
        body = client.get(f"/rss/test_feed.xml?key={_KEY}").content
    root = DET.fromstring(body)
    item = root.find("channel/item")
    enclosure = item.find("enclosure").get("url")
    assert f"key={_KEY}" in enclosure and "/media/ep.mp3" in enclosure
    transcript = item.find(f"{{{_PODCAST_NS}}}transcript").get("url")
    assert f"key={_KEY}" in transcript
    # Cover: key in the path token, and no query string at all.
    image = item.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}image").get("href")
    assert image.endswith(f"/media/ep-{_KEY}.jpg")
    assert "?" not in image


def test_rendered_urls_keyless_when_disabled(env: Path) -> None:
    _seed_episode(env)
    with _client(env) as client:
        body = client.get("/rss/test_feed.xml").content
    root = DET.fromstring(body)
    item = root.find("channel/item")
    assert "key=" not in item.find("enclosure").get("url")
    image = item.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}image").get("href")
    assert image.endswith("/media/ep.jpg")


def test_etag_changes_when_key_state_changes(env: Path) -> None:
    _seed_episode(env)
    with _client(env) as client:
        keyless = client.get("/rss/test_feed.xml").headers["etag"]
    _enable(env)
    with _client(env) as client:
        keyed = client.get(f"/rss/test_feed.xml?key={_KEY}").headers["etag"]
    assert keyless != keyed


# --- management endpoints --------------------------------------------------


def test_feed_auth_lifecycle(env: Path) -> None:
    with _client(env) as client:
        # Default: off, no key.
        status = client.get("/api/v1/feed-auth").json()
        assert status == {"enabled": False, "key": None, "keyed_feed_url": None}

        # Enable mints a key and returns a ready-to-copy keyed URL.
        enabled = client.post("/api/v1/feed-auth", json={"enabled": True}).json()
        assert enabled["enabled"] is True
        assert feed_auth.KEY_RE.fullmatch(enabled["key"])
        assert f"key={enabled['key']}" in enabled["keyed_feed_url"]
        minted = enabled["key"]

        # Re-enabling is idempotent -- the key is reused, not rotated.
        again = client.post("/api/v1/feed-auth", json={"enabled": True}).json()
        assert again["key"] == minted

        # Disable retains the key; re-enable reuses it.
        client.post("/api/v1/feed-auth", json={"enabled": False})
        reenabled = client.post("/api/v1/feed-auth", json={"enabled": True}).json()
        assert reenabled["key"] == minted


def test_regenerate_rotates_and_invalidates_old_key(env: Path) -> None:
    _seed_episode(env)
    with _client(env) as client:
        old = client.post("/api/v1/feed-auth", json={"enabled": True}).json()["key"]
        new = client.post("/api/v1/feed-auth/regenerate").json()["key"]
        assert new != old
        # The old key now 401s; the new key serves.
        assert client.get(f"/rss/test_feed.xml?key={old}").status_code == 401
        assert client.get(f"/rss/test_feed.xml?key={new}").status_code == 200


def test_regenerate_conflicts_when_disabled(env: Path) -> None:
    with _client(env) as client:
        assert client.post("/api/v1/feed-auth/regenerate").status_code == 409


# --- hardening (code-review findings) --------------------------------------


def test_generic_settings_put_cannot_write_feed_auth(env: Path) -> None:
    # The two keys are in ALLOWED_KEYS so the overlay reads them, but writing
    # them through the generic form (bypassing lazy key-gen) could enable auth
    # with no valid key and 401 the whole feed. The PUT must reject them.
    with _client(env) as client:
        assert client.put("/api/v1/settings", json={"FEED_AUTH_ENABLED": True}).status_code == 400
        assert client.put("/api/v1/settings", json={"FEED_AUTH_KEY": "x"}).status_code == 400


def test_enabling_forces_refetch_for_conditional_clients(env: Path) -> None:
    # Enabling changes the ETag but not last_build; a client re-polling with its
    # cached validators must get a fresh 200 (RFC 7232: If-None-Match wins over
    # If-Modified-Since), not a 304 that leaves it on the keyless feed.
    _seed_episode(env)
    with _client(env) as client:
        first = client.get("/rss/test_feed.xml")
        old_etag = first.headers["etag"]
        last_mod = first.headers["last-modified"]
    _enable(env)
    with _client(env) as client:
        resp = client.get(
            f"/rss/test_feed.xml?key={_KEY}",
            headers={"If-None-Match": old_etag, "If-Modified-Since": last_mod},
        )
    assert resp.status_code == 200


def test_conditional_get_still_304s_when_unchanged(env: Path) -> None:
    _seed_episode(env)
    with _client(env) as client:
        first = client.get("/rss/test_feed.xml")
        etag = first.headers["etag"]
        last_mod = first.headers["last-modified"]
        # Matching ETag -> 304.
        assert client.get("/rss/test_feed.xml", headers={"If-None-Match": etag}).status_code == 304
        # No ETag sent -> fall back to If-Modified-Since -> 304.
        assert (
            client.get(
                "/rss/test_feed.xml", headers={"If-Modified-Since": last_mod}
            ).status_code
            == 304
        )


def test_settings_feed_url_carries_key_when_enabled(env: Path) -> None:
    # The Feed page copies settings.feed_url; it must match the keyed subscribe
    # URL once auth is on, and revert to keyless when off.
    with _client(env) as client:
        assert "key=" not in client.get("/api/v1/settings").json()["feed_url"]
    _enable(env)
    with _client(env) as client:
        assert client.get("/api/v1/settings").json()["feed_url"].endswith(f"?key={_KEY}")


def test_set_if_absent_first_writer_wins(env: Path) -> None:
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        first = runtime_settings.set_if_absent(conn, "FEED_AUTH_KEY", _KEY)
        second = runtime_settings.set_if_absent(conn, "FEED_AUTH_KEY", _KEY2)
    finally:
        conn.close()
    assert first == _KEY
    assert second == _KEY  # the second write is ignored; the stored winner returns


def test_effective_auth_reads_env_default_and_override(env: Path) -> None:
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        settings = get_settings()
        # No stored rows: env/code defaults (disabled, no key).
        assert feed_auth.effective_auth(conn, settings) == (False, None)
        runtime_settings.set_value(conn, "FEED_AUTH_KEY", _KEY)
        runtime_settings.set_value(conn, "FEED_AUTH_ENABLED", True)
        assert feed_auth.effective_auth(conn, settings) == (True, _KEY)
    finally:
        conn.close()
