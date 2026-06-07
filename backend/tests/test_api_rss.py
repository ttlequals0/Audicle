from __future__ import annotations

from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path

import defusedxml.ElementTree as DET
from app.core import database
from app.main import create_app
from app.services import episodes
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


def test_get_rss_emits_etag(env: Path) -> None:
    _seed(env)
    with _client(env) as client:
        response = client.get("/rss/test_feed.xml")
    assert response.headers.get("etag")


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
