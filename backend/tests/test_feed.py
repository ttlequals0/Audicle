from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import defusedxml.ElementTree as DET
import pytest
from app.config import get_settings
from app.services import feed
from app.services.episodes import Episode

_PODCAST_NS = "https://podcastindex.org/namespace/1.0"
_ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"


def _episode(
    *,
    id: str = "abc123",
    title: str = "An Article",
    author: str = "Author Name",
    audio_path: str | None = None,
    artwork_path: str | None = None,
    transcript_vtt: str | None = "WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.000\nhi\n",
    duration_secs: int | None = 90,
    pub_date: str = "2026-05-28T18:00:00Z",
    summary: str | None = None,
    revision: int = 1,
) -> Episode:
    return Episode(
        id=id,
        job_id="job1",
        title=title,
        author=author,
        original_url=f"https://example.test/{id}",
        audio_path=audio_path,
        artwork_path=artwork_path,
        transcript_vtt=transcript_vtt,
        duration_secs=duration_secs,
        pub_date=pub_date,
        created_at=pub_date,
        updated_at=pub_date,
        summary=summary,
        revision=revision,
    )


def _last_build() -> datetime:
    return datetime(2026, 5, 28, 18, 0, 0, tzinfo=UTC)


def _render(
    episodes: list[Episode],
    *,
    env: Path,
    podcast_guid: str = "11111111-2222-3333-4444-555555555555",
) -> bytes:
    return feed.render(
        episodes,
        settings=get_settings(),
        podcast_guid=podcast_guid,
        last_build=_last_build(),
    )


def test_channel_contains_required_fields(env: Path) -> None:
    body = _render([], env=env)
    root = DET.fromstring(body)
    channel = root.find("channel")
    assert channel is not None
    assert channel.find("title").text == get_settings().FEED_TITLE
    assert channel.find("description").text == get_settings().FEED_DESCRIPTION
    assert channel.find("language").text == get_settings().FEED_LANGUAGE
    # Channel cover is extension-clean (no ?v=) so podcast apps accept it.
    assert channel.find("image/url").text == get_settings().FEED_ARTWORK_URL


def test_raw_github_url_rewrites_blob_to_raw() -> None:
    assert (
        feed._raw_github_url(
            "https://github.com/ttlequals0/Audicle/blob/main/branding/podcast-artwork-3000.jpg"
        )
        == "https://raw.githubusercontent.com/ttlequals0/Audicle/main/branding/podcast-artwork-3000.jpg"
    )
    # Query/hash stripped so the cover stays extension-clean for Apple.
    assert (
        feed._raw_github_url("https://github.com/o/r/blob/main/a.jpg?raw=true")
        == "https://raw.githubusercontent.com/o/r/main/a.jpg"
    )
    # Non-blob URLs pass through unchanged.
    raw = "https://raw.githubusercontent.com/o/r/main/a.jpg"
    assert feed._raw_github_url(raw) == raw
    assert feed._raw_github_url("https://example.com/cover.png") == "https://example.com/cover.png"


def test_channel_artwork_rewrites_github_blob_url(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A pasted GitHub blob page URL (HTML) is rewritten to the raw image URL so
    # the cover actually resolves in podcast apps.
    monkeypatch.setenv(
        "FEED_ARTWORK_URL",
        "https://github.com/ttlequals0/Audicle/blob/main/branding/podcast-artwork-3000.jpg",
    )
    get_settings.cache_clear()
    channel = DET.fromstring(_render([], env=env)).find("channel")
    assert (
        channel.find("image/url").text
        == "https://raw.githubusercontent.com/ttlequals0/Audicle/main/branding/podcast-artwork-3000.jpg"
    )


def test_channel_contains_podcast_namespace_tags(env: Path) -> None:
    body = _render([], env=env, podcast_guid="abcdef-guid")
    root = DET.fromstring(body)
    channel = root.find("channel")
    assert channel.find(f"{{{_PODCAST_NS}}}guid").text == "abcdef-guid"
    assert channel.find(f"{{{_PODCAST_NS}}}locked").text == "yes"
    assert channel.find(f"{{{_PODCAST_NS}}}medium").text == "podcast"
    txt = channel.find(f"{{{_PODCAST_NS}}}txt")
    assert txt.text and "AI" in txt.text
    assert txt.get("purpose") == "ai-content"


def test_channel_pc2_tags_precede_items(env: Path) -> None:
    """The PC2 channel-level tags must come BEFORE the first <item> per the
    Podcast Index reference feeds; some validators warn otherwise."""

    ep = _episode()
    body = _render([ep], env=env)
    root = DET.fromstring(body)
    channel = root.find("channel")
    first_item_idx = next(i for i, child in enumerate(channel) if child.tag == "item")
    pc2_guid_idx = next(
        i for i, child in enumerate(channel) if child.tag == f"{{{_PODCAST_NS}}}guid"
    )
    assert pc2_guid_idx < first_item_idx


def test_channel_includes_itunes_type_episodic(env: Path) -> None:
    body = _render([], env=env)
    root = DET.fromstring(body)
    itunes_type = root.find(f"channel/{{{_ITUNES_NS}}}type")
    assert itunes_type is not None
    assert itunes_type.text == "episodic"


def test_channel_image_has_required_subelements(env: Path) -> None:
    """RSS 2.0 requires <url>, <title>, <link> under <image>; validators
    reject the channel image otherwise."""

    body = _render([], env=env)
    root = DET.fromstring(body)
    image = root.find("channel/image")
    assert image is not None
    assert image.find("url") is not None
    assert image.find("title") is not None
    assert image.find("link") is not None


def test_channel_link_points_at_base_url_not_feed_url(env: Path) -> None:
    """feedgen's channel <link> tracks the LAST fg.link() call. Ensure the
    rendered channel link is BASE_URL (the website), not the feed itself."""

    body = _render([], env=env)
    root = DET.fromstring(body)
    channel_link = root.find("channel/link")
    assert channel_link is not None
    assert channel_link.text == get_settings().BASE_URL


def test_item_enclosure_uses_audio_path_filesize(env: Path, tmp_path: Path) -> None:
    mp3 = tmp_path / "abc.mp3"
    mp3.write_bytes(b"FAKE_MP3_BODY")  # 13 bytes
    ep = _episode(id="abc", audio_path=str(mp3))
    body = _render([ep], env=env)
    root = DET.fromstring(body)
    enclosure = root.find("channel/item/enclosure")
    assert enclosure is not None
    assert enclosure.get("type") == "audio/mpeg"
    assert int(enclosure.get("length")) == 13
    assert "/media/abc.mp3?v=" in enclosure.get("url")


def test_item_enclosure_missing_file_reports_zero_length(env: Path) -> None:
    ep = _episode(id="ghost", audio_path="/data/media/ghost.mp3")
    body = _render([ep], env=env)
    root = DET.fromstring(body)
    enclosure = root.find("channel/item/enclosure")
    assert int(enclosure.get("length")) == 0


def test_item_guid_stable_on_first_render(env: Path) -> None:
    ep = _episode(id="abc", revision=1)
    body = _render([ep], env=env)
    guid = DET.fromstring(body).find("channel/item/guid")
    assert guid.text == "abc"


def test_item_guid_gets_revision_suffix_after_reprocess(env: Path) -> None:
    # A reprocessed episode (revision > 1) gets a fresh GUID so clients re-download.
    ep = _episode(id="abc", revision=3)
    body = _render([ep], env=env)
    guid = DET.fromstring(body).find("channel/item/guid")
    assert guid.text == "abc-r3"


def test_item_includes_itunes_duration_in_hms(env: Path) -> None:
    ep = _episode(duration_secs=3725)  # 1h 02m 05s
    body = _render([ep], env=env)
    root = DET.fromstring(body)
    duration_el = root.find(f"channel/item/{{{_ITUNES_NS}}}duration")
    assert duration_el is not None
    assert duration_el.text == "01:02:05"


def test_item_includes_podcast_transcript_when_vtt_present(env: Path) -> None:
    ep = _episode(transcript_vtt="WEBVTT\n")
    body = _render([ep], env=env)
    root = DET.fromstring(body)
    transcript = root.find(f"channel/item/{{{_PODCAST_NS}}}transcript")
    assert transcript is not None
    assert transcript.get("type") == "text/vtt"
    assert transcript.get("language") == get_settings().FEED_LANGUAGE
    assert transcript.get("rel") == "captions"
    assert f"/media/{ep.id}.vtt?v=" in transcript.get("url")


def test_item_omits_podcast_transcript_when_no_vtt(env: Path) -> None:
    ep = _episode(transcript_vtt=None)
    body = _render([ep], env=env)
    root = DET.fromstring(body)
    assert root.find(f"channel/item/{{{_PODCAST_NS}}}transcript") is None


def test_item_artwork_falls_back_to_feed_when_no_jpg(env: Path) -> None:
    ep = _episode(artwork_path=None)
    body = _render([ep], env=env)
    root = DET.fromstring(body)
    image = root.find(f"channel/item/{{{_ITUNES_NS}}}image")
    assert image is not None
    assert image.get("href") == get_settings().FEED_ARTWORK_URL


def test_item_description_and_summary_include_show_notes(env: Path) -> None:
    note = "This article walks through the Linux kernel boot sequence."
    ep = _episode(summary=note)
    body = _render([ep], env=env)
    root = DET.fromstring(body)
    assert note in (root.find("channel/item/description").text or "")
    assert note in (root.find(f"channel/item/{{{_ITUNES_NS}}}summary").text or "")


def test_item_description_omits_summary_when_absent(env: Path) -> None:
    ep = _episode(summary=None)
    body = _render([ep], env=env)
    root = DET.fromstring(body)
    # No summary -> description is just the title/author/source, no empty <p></p>.
    assert "<p></p>" not in (root.find("channel/item/description").text or "")


def test_item_artwork_falls_back_to_default_when_feed_url_unset(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: an unset FEED_ARTWORK_URL is "", which feedgen rejects with
    # "Image file must be png or jpg", crashing the whole render with a 500. The
    # per-item image must fall back to the branded DEFAULT_ARTWORK_URL instead.
    monkeypatch.setenv("FEED_ARTWORK_URL", "")
    get_settings.cache_clear()
    ep = _episode(artwork_path=None)
    body = _render([ep], env=env)
    root = DET.fromstring(body)
    image = root.find(f"channel/item/{{{_ITUNES_NS}}}image")
    assert image is not None
    assert image.get("href") == get_settings().DEFAULT_ARTWORK_URL


def test_artwork_falls_back_to_local_default_when_both_urls_unset(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both operator and branded defaults empty -> feed must still emit a
    # non-empty .jpg (the seeded /media/default.jpg) rather than crash feedgen
    # with "Image file must be png or jpg".
    monkeypatch.setenv("FEED_ARTWORK_URL", "")
    monkeypatch.setenv("DEFAULT_ARTWORK_URL", "")
    get_settings.cache_clear()
    assert get_settings().DEFAULT_ARTWORK_URL == ""
    ep = _episode(artwork_path=None)
    body = _render([ep], env=env)
    root = DET.fromstring(body)
    channel_image = root.find("channel/image/url")
    assert channel_image is not None
    assert channel_image.text.endswith("/media/default.jpg")
    item_image = root.find(f"channel/item/{{{_ITUNES_NS}}}image")
    assert item_image is not None
    assert item_image.get("href").endswith("/media/default.jpg")


def test_item_artwork_links_per_episode_jpg_when_present(env: Path) -> None:
    ep = _episode(artwork_path="/data/media/abc.jpg")
    body = _render([ep], env=env)
    root = DET.fromstring(body)
    image = root.find(f"channel/item/{{{_ITUNES_NS}}}image")
    # Extension-clean (no ?v=): Apple/podcast apps require the URL to end in .jpg.
    assert image.get("href").endswith(f"/media/{ep.id}.jpg")


def test_media_cache_buster_tracks_updated_at(env: Path, tmp_path: Path) -> None:
    """The audio enclosure and transcript carry ?v=<epoch> that changes when
    updated_at changes (so apps re-download after a reprocess), while the artwork
    URL stays extension-clean (no ?v=) so apps accept it."""

    mp3 = tmp_path / "abc.mp3"
    mp3.write_bytes(b"x")

    def _urls(updated_at: str) -> tuple[str, str, str]:
        ep = Episode(
            id="abc",
            job_id="job1",
            title="T",
            author="A",
            original_url="https://example.test/abc",
            audio_path=str(mp3),
            artwork_path="/data/media/abc.jpg",
            transcript_vtt="WEBVTT\n",
            duration_secs=10,
            pub_date="2026-05-28T18:00:00Z",
            created_at="2026-05-28T18:00:00Z",
            updated_at=updated_at,
            summary=None,
        )
        root = DET.fromstring(_render([ep], env=env))
        enc = root.find("channel/item/enclosure").get("url")
        img = root.find(f"channel/item/{{{_ITUNES_NS}}}image").get("href")
        vtt = root.find(f"channel/item/{{{_PODCAST_NS}}}transcript").get("url")
        return enc, img, vtt

    enc1, img1, vtt1 = _urls("2026-05-28T18:00:00Z")
    enc2, img2, vtt2 = _urls("2026-05-28T19:30:00Z")
    assert "?v=" in enc1 and "?v=" in vtt1
    assert "?v=" not in img1 and img1.endswith("/media/abc.jpg")
    assert enc1 != enc2 and vtt1 != vtt2  # audio/transcript bust on reprocess
    assert img1 == img2  # artwork URL stable + extension-clean


def test_channel_cover_is_extension_clean(env: Path) -> None:
    """The channel cover itunes:image must end in .jpg/.png with NO ?v= query;
    Apple and several apps reject artwork URLs that end in a query string."""

    body = _render([_episode()], env=env)
    root = DET.fromstring(body)
    href = root.find(f"channel/{{{_ITUNES_NS}}}image").get("href")
    assert "?v=" not in href
    assert href.rsplit(".", 1)[-1] in {"jpg", "png"}


def test_self_link_points_at_rss_endpoint(env: Path) -> None:
    body = _render([], env=env)
    root = DET.fromstring(body)
    atom_self = root.find("channel/{http://www.w3.org/2005/Atom}link[@rel='self']")
    assert atom_self is not None
    assert atom_self.get("href").endswith("/rss/test_feed.xml")


def test_hms_handles_zero_and_negative(env: Path) -> None:
    assert feed._hms(0) == "00:00:00"
    assert feed._hms(-5) == "00:00:00"
    assert feed._hms(59) == "00:00:59"
    assert feed._hms(3600) == "01:00:00"
    assert feed._hms(86399) == "23:59:59"


def test_item_guid_is_bare_episode_id_without_epoch(env: Path) -> None:
    """Back-compat: epoch 0 (never rotated) leaves item guids as the bare id."""

    body = _render([_episode(id="abc123")], env=env)
    item = DET.fromstring(body).find("channel").find("item")
    assert item.find("guid").text == "abc123"


def test_item_guid_salted_with_feed_guid_epoch(env: Path) -> None:
    """After a force-recreate the item guid carries the epoch suffix so apps
    treat the episode as new; the media enclosure URL keeps the bare id."""

    body = feed.render(
        [_episode(id="abc123", audio_path="/data/media/abc123.mp3")],
        settings=get_settings(),
        podcast_guid="g",
        last_build=_last_build(),
        feed_guid_epoch=3,
    )
    item = DET.fromstring(body).find("channel").find("item")
    assert item.find("guid").text == "abc123-3"
    # Enclosure still points at the real file (bare id, not salted).
    assert "abc123.mp3" in item.find("enclosure").get("url")


def test_parse_iso_handles_z_suffix() -> None:
    result = feed._parse_iso("2026-05-28T18:00:00Z")
    assert result is not None
    assert result.tzinfo is not None


def test_parse_iso_returns_none_for_garbage(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="app.services.feed"):
        assert feed._parse_iso("not-a-date") is None
    assert any(getattr(rec, "event", "") == "feed_timestamp_parse_failed" for rec in caplog.records)
