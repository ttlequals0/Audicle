"""Feed-name slug. Turns the operator's ``FEED_TITLE`` into a filesystem/URL-safe
token used as the RSS path (e.g. "Articles of Interest" -> ``articles_of_interest``,
served at ``/rss/articles_of_interest.xml``).

Underscore-separated (matching the user's example), lowercase, ASCII-folded. A
title that reduces to nothing (empty, or all non-ASCII) falls back to a default
so the feed always has a usable URL.
"""

from __future__ import annotations

import re
import unicodedata

_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")
# The single source of truth for the feed-name default and slug rule. Used
# wherever an effective FEED_TITLE or feed URL is needed (rss route, feed
# render, settings response) so the default and format can't drift.
DEFAULT_FEED_TITLE = "Audicle"
_DEFAULT_SLUG = "audicle"
_MAX_LEN = 80


def slugify(name: str) -> str:
    """Slugify ``name`` to ``[a-z0-9_]`` with underscore separators.

    Runs of non-alphanumerics collapse to a single underscore; leading/trailing
    underscores are stripped; the result is capped at 80 chars; an empty result
    falls back to ``"audicle"``.
    """

    ascii_text = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    )
    slug = _NON_SLUG_RE.sub("_", ascii_text.lower())[:_MAX_LEN].strip("_")
    return slug or _DEFAULT_SLUG


def feed_slug(title: str | None) -> str:
    """The slug for a feed title, applying the default when title is empty/None."""

    return slugify(title or DEFAULT_FEED_TITLE)


def feed_url(base_url: str, title: str | None) -> str:
    """The public RSS URL for a feed title: ``{base_url}/rss/{slug}.xml``."""

    return f"{base_url.rstrip('/')}/rss/{feed_slug(title)}.xml"
