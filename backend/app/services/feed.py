"""RSS feed generation per build-plan.md Phase 7.

feedgen renders the Atom + iTunes namespaces; the Podcasting 2.0 (``podcast:``)
namespace is layered on via string-level XML construction afterwards because
feedgen doesn't model it natively. That follows the MinusPod precedent.

Channel fields come from ``Settings`` (operator env vars); per-episode fields
come from the ``episodes`` table. ``podcast:guid`` is persisted via
``settings_store.get_or_init_podcast_guid`` so the identifier is stable across
restarts and feed-URL changes.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import defusedxml.ElementTree as DET
from feedgen.feed import FeedGenerator

from app.config import Settings
from app.services.episodes import Episode

logger = logging.getLogger("app.services.feed")

_PODCAST_NS = "https://podcastindex.org/namespace/1.0"
_ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
_ATOM_NS = "http://www.w3.org/2005/Atom"

# Register the namespace prefixes ONCE at module load so the round-trip
# through ET.tostring re-emits ``podcast:`` / ``itunes:`` / ``atom:`` instead
# of stdlib ElementTree's auto-assigned ``ns0:`` / ``ns1:`` prefixes. Apple
# Podcasts and Cast Feed Validator reject the auto-prefixed forms.
ET.register_namespace("podcast", _PODCAST_NS)
ET.register_namespace("itunes", _ITUNES_NS)
ET.register_namespace("atom", _ATOM_NS)


def render(
    episodes: list[Episode],
    *,
    settings: Settings,
    podcast_guid: str,
    last_build: datetime,
) -> bytes:
    """Render the full RSS document (channel + items + PC2 tags) as bytes.

    ``last_build`` is the timestamp the operator wants to advertise as
    ``<lastBuildDate>``; the caller derives it from the newest episode's
    ``updated_at`` (and falls back to ``now``).
    """

    fg = FeedGenerator()
    fg.load_extension("podcast")  # iTunes namespace shortcut

    fg.title(settings.FEED_TITLE)
    fg.description(settings.FEED_DESCRIPTION)
    fg.author({"name": settings.FEED_AUTHOR, "email": settings.FEED_EMAIL})
    fg.language(settings.FEED_LANGUAGE)
    # Order matters: feedgen's channel ``<link>`` is bound to the LAST link()
    # call. Call the atom ``rel="self"`` first so the channel ``<link>``
    # element ends up pointing at BASE_URL (the website) rather than at the
    # feed URL itself, which is the conventional rendering for podcast
    # players.
    fg.link(href=f"{settings.BASE_URL.rstrip('/')}/rss/rss.xml", rel="self")
    fg.link(href=settings.BASE_URL, rel="alternate")
    # Legacy ``<image>`` needs ``<title>`` and ``<link>`` per RSS 2.0 to
    # validate; the build plan calls these out explicitly.
    fg.image(
        url=settings.FEED_ARTWORK_URL,
        title=settings.FEED_TITLE,
        link=settings.BASE_URL,
    )
    fg.lastBuildDate(last_build)
    fg.podcast.itunes_author(settings.FEED_AUTHOR)
    fg.podcast.itunes_owner(name=settings.FEED_AUTHOR, email=settings.FEED_EMAIL)
    fg.podcast.itunes_category(settings.FEED_CATEGORY)
    fg.podcast.itunes_image(settings.FEED_ARTWORK_URL)
    fg.podcast.itunes_explicit("yes" if settings.FEED_EXPLICIT else "no")
    fg.podcast.itunes_summary(settings.FEED_DESCRIPTION)
    # Tell Apple Podcasts this is an episodic (not serial) feed so the UI
    # displays newest-first.
    fg.podcast.itunes_type("episodic")

    if episodes:
        # pubDate of the channel mirrors the newest episode per the build plan.
        newest_pub = _parse_iso(episodes[0].pub_date)
        if newest_pub is not None:
            fg.pubDate(newest_pub)

    for ep in episodes:
        item = fg.add_entry(order="append")
        item.id(ep.id)
        item.guid(ep.id, permalink=False)
        if ep.title:
            item.title(ep.title)
        if ep.author:
            item.author({"name": ep.author})
        item.link(href=ep.original_url)
        item.description(ep.title or ep.original_url)
        item.pubDate(_parse_iso(ep.pub_date) or last_build)
        if ep.audio_path:
            audio_url = _media_url(settings.BASE_URL, ep.id, "mp3")
            item.enclosure(
                url=audio_url,
                length=str(_safe_filesize(ep.audio_path)),
                type="audio/mpeg",
            )
        if ep.duration_secs is not None:
            item.podcast.itunes_duration(_hms(ep.duration_secs))
        item.podcast.itunes_image(
            _media_url(settings.BASE_URL, ep.id, "jpg")
            if ep.artwork_path
            else settings.FEED_ARTWORK_URL
        )
        item.podcast.itunes_explicit("yes" if settings.FEED_EXPLICIT else "no")

    base_xml = fg.rss_str(pretty=False)
    return _inject_pc2_tags(
        base_xml,
        podcast_guid=podcast_guid,
        episodes=episodes,
        settings=settings,
    )


def _inject_pc2_tags(
    xml_bytes: bytes,
    *,
    podcast_guid: str,
    episodes: list[Episode],
    settings: Settings,
) -> bytes:
    """Append the PC2 namespace + channel-level + per-item PC2 elements.

    feedgen doesn't natively understand the ``podcast:`` namespace, so we
    re-parse the rendered RSS, register the namespace on the ``<rss>`` root,
    add channel-level PC2 tags (``guid``, ``txt purpose="ai-content"``,
    ``locked``), and a per-item ``podcast:transcript`` element pointing at
    the VTT handler.
    """

    # ``defusedxml.ElementTree.fromstring`` blocks XXE / billion-laughs / DTD
    # retrieval; the stdlib parser would happily expand them. feedgen's own
    # output never contains attacker input (we control every channel/item
    # field), but defense-in-depth is cheap.
    root = DET.fromstring(xml_bytes)
    channel = root.find("channel")
    if channel is None:
        return xml_bytes  # unreachable but keeps the type-checker happy

    # PC2 channel-level tags must precede ``<item>`` elements per the
    # Podcast Index reference feeds; some validators warn otherwise. Build
    # the tags up front, then ``insert`` them before the first item.
    insert_at = _first_item_index(channel)

    pc2_medium = ET.Element(f"{{{_PODCAST_NS}}}medium")
    pc2_medium.text = "podcast"
    channel.insert(insert_at, pc2_medium)
    insert_at += 1

    pc2_guid = ET.Element(f"{{{_PODCAST_NS}}}guid")
    pc2_guid.text = podcast_guid
    channel.insert(insert_at, pc2_guid)
    insert_at += 1

    pc2_locked = ET.Element(f"{{{_PODCAST_NS}}}locked")
    pc2_locked.text = "yes"
    pc2_locked.set("owner", settings.FEED_EMAIL)
    channel.insert(insert_at, pc2_locked)
    insert_at += 1

    # ``podcast:txt`` is free-form text gated by the ``purpose`` attribute.
    # Carry a human-readable disclosure so PC2-aware clients (Fountain,
    # Podverse) can surface the AI-content flag without parsing a sentinel.
    pc2_txt = ET.Element(f"{{{_PODCAST_NS}}}txt")
    pc2_txt.text = "This podcast contains AI-generated narration via TTS."
    pc2_txt.set("purpose", "ai-content")
    channel.insert(insert_at, pc2_txt)

    items = channel.findall("item")
    # ``strict=True`` makes the assertion ``items align with episodes`` a
    # hard contract: if a future feedgen change drops or reorders entries
    # the build fails loudly rather than silently shipping wrong-episode
    # transcripts.
    for item_el, ep in zip(items, episodes, strict=True):
        if not ep.transcript_vtt:
            continue
        transcript_url = _media_url(settings.BASE_URL, ep.id, "vtt")
        ET.SubElement(
            item_el,
            f"{{{_PODCAST_NS}}}transcript",
            attrib={
                "url": transcript_url,
                "type": "text/vtt",
                "language": settings.FEED_LANGUAGE,
                "rel": "captions",
            },
        )

    # ``xml_declaration=True`` matches feedgen's default rss_str() output so
    # the response body has the same prolog clients are used to.
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def _first_item_index(channel: ET.Element) -> int:
    """Index of the first ``<item>`` child, or len(channel) if there are
    none -- where PC2 channel-level tags should be inserted."""

    for index, child in enumerate(channel):
        if child.tag == "item":
            return index
    return len(channel)


def _media_url(base_url: str, episode_id: str, ext: str) -> str:
    return f"{base_url.rstrip('/')}/media/{episode_id}.{ext}"


def _hms(seconds: int) -> str:
    """Render integer seconds as ``HH:MM:SS`` per itunes:duration spec."""

    secs = max(0, seconds)
    hours, remainder = divmod(secs, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _parse_iso(value: str) -> datetime | None:
    """Parse the ISO-8601 timestamps SQLite emits via ``strftime``.

    feedgen requires tz-aware datetimes; the DB values end in ``Z`` so we
    swap that for ``+00:00`` to satisfy ``datetime.fromisoformat`` on
    Python < 3.11 hosts. On 3.11+ the literal ``Z`` parses but the swap is
    harmless.
    """

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logger.warning(
            "Could not parse episode timestamp",
            extra={"event": "feed_timestamp_parse_failed", "value": value},
        )
        return None


def _safe_filesize(path_str: str) -> int:
    """Best-effort enclosure length. Missing files report 0 so the feed
    still validates rather than 500-ing on a stale row."""

    try:
        return Path(path_str).stat().st_size
    except OSError:
        return 0
