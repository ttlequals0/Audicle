from __future__ import annotations

from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path

import defusedxml.ElementTree as DET
from app.config import get_settings
from app.core import database
from app.main import create_app
from app.services import episodes, feed, settings_store
from fastapi.testclient import TestClient

_PODCAST_NS = "https://podcastindex.org/namespace/1.0"


def _client(env: Path) -> TestClient:
    database.run_migrations(env)
    return TestClient(create_app())


def _seed(env: Path, *, audio_path: str = "/data/media/ep.mp3") -> None:
    database.run_migrations(env)
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id="ep",
            job_id=None,
            original_url="https://example.test/a",
            title="An Article",
            author="Author Name",
            audio_path=audio_path,
            artwork_path=None,
            transcript_vtt="WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.000\nhi\n",
            duration_secs=42,
        )
        conn.execute(
            "UPDATE episodes SET pub_date='2026-05-28T18:00:00Z', "
            "updated_at='2026-05-28T18:00:00Z' WHERE id='ep'"
        )
        conn.commit()
    finally:
        conn.close()


def test_get_rss_returns_200_with_xml_body(env: Path) -> None:
    _seed(env)
    with _client(env) as client:
        response = client.get("/rss/test_feed.xml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/rss+xml")
    root = DET.fromstring(response.content)
    assert root.tag == "rss"
    items = root.findall("channel/item")
    assert len(items) == 1


def test_feed_projection_renders_same_xml_as_full_rows(env: Path) -> None:
    _seed(env)
    with database.connection(env) as conn:
        settings = get_settings()
        guid = settings_store.get_or_init_podcast_guid(conn, settings.BASE_URL)
        kwargs = {
            "settings": settings,
            "podcast_guid": guid,
            "last_build": parsedate_to_datetime("Thu, 28 May 2026 18:00:00 GMT"),
            "feed_guid_epoch": settings_store.get_feed_guid_epoch(conn),
        }
        full = feed.render(episodes.list_published(conn), **kwargs)
        projected = feed.render(episodes.list_for_feed(conn), **kwargs)
    assert projected == full


def test_head_rss_returns_200_with_headers_and_no_body(env: Path) -> None:
    # Apple Podcasts and other platforms issue HEAD before GET; the route must
    # answer HEAD with 200 + headers, not 405 (feed-validator FATAL).
    _seed(env)
    with _client(env) as client:
        response = client.head("/rss/test_feed.xml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/rss+xml")
    assert response.headers.get("last-modified")
    assert response.headers.get("etag")
    assert response.content == b""


def test_head_and_conditional_requests_skip_episode_projection(
    env: Path, monkeypatch
) -> None:
    _seed(env)
    with _client(env) as client:
        first = client.get("/rss/test_feed.xml")

        def _unexpected(_conn):
            raise AssertionError("episode projection should not be loaded")

        monkeypatch.setattr(episodes, "list_for_feed", _unexpected)
        head = client.head("/rss/test_feed.xml")
        rendered_cache_hit = client.get("/rss/test_feed.xml")
        cached = client.get(
            "/rss/test_feed.xml", headers={"If-None-Match": first.headers["etag"]}
        )
    assert head.status_code == 200
    assert rendered_cache_hit.status_code == 200
    assert rendered_cache_hit.content == first.content
    assert cached.status_code == 304


def test_get_rss_emits_etag(env: Path) -> None:
    _seed(env)
    with _client(env) as client:
        response = client.get("/rss/test_feed.xml")
    assert response.headers.get("etag")


def test_get_rss_weak_if_none_match_uses_weak_comparison(env: Path) -> None:
    _seed(env)
    with _client(env) as client:
        first = client.get("/rss/test_feed.xml")
        response = client.get(
            "/rss/test_feed.xml",
            headers={"If-None-Match": f"W/{first.headers['etag']}"},
        )
    assert response.status_code == 304


def test_get_rss_emits_cache_control_header(env: Path) -> None:
    _seed(env)
    with _client(env) as client:
        response = client.get("/rss/test_feed.xml")
    cc = response.headers["cache-control"]
    assert "public" in cc
    assert "max-age=" in cc


def test_get_rss_last_modified_round_trips_to_304(env: Path) -> None:
    _seed(env)
    with _client(env) as client:
        first = client.get("/rss/test_feed.xml")
        last_modified = first.headers["last-modified"]
        # Reuse the Last-Modified value as If-Modified-Since; expect 304.
        not_modified = client.get(
            "/rss/test_feed.xml",
            headers={"If-Modified-Since": last_modified},
        )
    assert first.status_code == 200
    assert not_modified.status_code == 304
    assert not_modified.headers["last-modified"] == last_modified
    assert not_modified.content == b""


def test_feed_metadata_change_invalidates_etag_and_last_modified(env: Path) -> None:
    _seed(env)
    with _client(env) as client:
        first = client.get("/rss/test_feed.xml")
        changed = client.put(
            "/api/v1/settings", json={"FEED_DESCRIPTION": "A changed description"}
        )
        assert changed.status_code == 200
        refreshed = client.get(
            "/rss/test_feed.xml",
            headers={
                "If-None-Match": first.headers["etag"],
                "If-Modified-Since": first.headers["last-modified"],
            },
        )
        ims_only = client.get(
            "/rss/test_feed.xml",
            headers={"If-Modified-Since": first.headers["last-modified"]},
        )
    assert refreshed.status_code == 200
    assert ims_only.status_code == 200
    assert refreshed.headers["etag"] != first.headers["etag"]


def test_environment_feed_change_invalidates_after_restart(env: Path, monkeypatch) -> None:
    _seed(env)
    with _client(env) as client:
        first = client.get("/rss/test_feed.xml")
    monkeypatch.setenv("FEED_DESCRIPTION", "Changed through the environment")
    get_settings.cache_clear()
    with _client(env) as client:
        refreshed = client.get(
            "/rss/test_feed.xml", headers={"If-None-Match": first.headers["etag"]}
        )
    assert refreshed.status_code == 200
    assert refreshed.headers["etag"] != first.headers["etag"]


def test_get_rss_returns_full_body_when_client_is_older(env: Path) -> None:
    _seed(env)
    with _client(env) as client:
        old = format_datetime(parsedate_to_datetime("Sun, 01 Jan 2000 00:00:00 GMT"), usegmt=True)
        response = client.get("/rss/test_feed.xml", headers={"If-Modified-Since": old})
    assert response.status_code == 200
    assert len(response.content) > 0


def test_get_rss_persists_podcast_guid_across_requests(env: Path) -> None:
    _seed(env)
    with _client(env) as client:
        first = client.get("/rss/test_feed.xml")
        second = client.get("/rss/test_feed.xml")
    g1 = DET.fromstring(first.content).find(f"channel/{{{_PODCAST_NS}}}guid").text
    g2 = DET.fromstring(second.content).find(f"channel/{{{_PODCAST_NS}}}guid").text
    assert g1 == g2


def test_get_rss_wrong_slug_404s(env: Path) -> None:
    # The feed lives only at the current FEED_TITLE slug ("Audicle" ->
    # /rss/test_feed.xml). The legacy /rss/rss.xml and any other slug 404.
    _seed(env)
    with _client(env) as client:
        assert client.get("/rss/rss.xml").status_code == 404
        assert client.get("/rss/something_else.xml").status_code == 404
        assert client.get("/rss/test_feed.xml").status_code == 200


def test_get_rss_self_link_uses_current_slug(env: Path) -> None:
    _seed(env)
    with _client(env) as client:
        response = client.get("/rss/test_feed.xml")
    root = DET.fromstring(response.content)
    atom_self = root.find("channel/{http://www.w3.org/2005/Atom}link[@rel='self']")
    assert atom_self.get("href").endswith("/rss/test_feed.xml")


def test_get_rss_with_no_episodes_returns_200_empty_channel(env: Path) -> None:
    with _client(env) as client:
        response = client.get("/rss/test_feed.xml")
    assert response.status_code == 200
    root = DET.fromstring(response.content)
    assert len(root.findall("channel/item")) == 0


def test_get_rss_excludes_episodes_with_null_audio(env: Path) -> None:
    _seed(env, audio_path="/data/media/ep.mp3")
    conn = database.connect(database.db_path(env))
    try:
        episodes.upsert(
            conn,
            id="half",
            job_id=None,
            original_url="https://example.test/half",
            title="Half-baked",
            author="A",
            audio_path=None,
            artwork_path=None,
            transcript_vtt=None,
            duration_secs=None,
        )
    finally:
        conn.close()
    with _client(env) as client:
        response = client.get("/rss/test_feed.xml")
    root = DET.fromstring(response.content)
    guids = [g.text for g in root.findall("channel/item/guid")]
    # Only the complete "ep" episode appears (the audio-less "half" is excluded);
    # the guid carries the updated_at version token, so compare the base id.
    assert len(guids) == 1 and guids[0].split("-v")[0] == "ep"
