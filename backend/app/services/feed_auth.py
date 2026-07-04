"""Optional global feed-key auth for the public feed + media surface.

When ``FEED_AUTH_ENABLED`` is on, every public feed/asset URL requires the
global ``FEED_AUTH_KEY``. RSS, mp3, vtt, and txt carry it as ``?key=``; cover
art embeds it in the path token (``/media/<episode_id>-<key>.jpg``) because
podcast apps and CDNs reject image URLs that end in a query string. The key is
64 lowercase hex chars (``secrets.token_hex(32)``); hex has no hyphens, so the
cover token splits unambiguously on the last hyphen.

Both settings live in the runtime-settings overlay, read per request (no
caching) so a rotation applies with no restart. This module holds the pure
helpers; the route guard is ``app.api.deps.require_feed_key``.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import sqlite3

from app.config import Settings

# A stored key is always 64 lowercase hex chars. The serving side uses this to
# tell a cover token's optional key suffix from the episode id (hyphen-free hex)
# and to prefilter a supplied key before the constant-time compare.
KEY_RE = re.compile(r"[0-9a-f]{64}")


def generate_key() -> str:
    """64 lowercase hex chars; the charset keeps the cover token unambiguous."""

    return secrets.token_hex(32)


def effective_auth(conn: sqlite3.Connection, settings: Settings) -> tuple[bool, str | None]:
    """Effective ``(enabled, key)`` for the route guard.

    A targeted read of just the two feed-auth rows on the request-scoped
    connection, with the env/code ``Settings`` as the fallback -- cheaper than a
    full ``runtime_settings.overlay()`` on every media request (no new connection,
    no all-rows coercion), and it reuses the handler's connection. Read per
    request so a rotation applies with no restart.
    """

    rows = conn.execute(
        "SELECT key, value FROM runtime_settings WHERE key IN (?, ?)",
        ("FEED_AUTH_ENABLED", "FEED_AUTH_KEY"),
    ).fetchall()
    stored = {row["key"]: row["value"] for row in rows}
    enabled = settings.FEED_AUTH_ENABLED
    if "FEED_AUTH_ENABLED" in stored:
        try:
            enabled = bool(json.loads(stored["FEED_AUTH_ENABLED"]))
        except (TypeError, ValueError):
            enabled = stored["FEED_AUTH_ENABLED"].strip().lower() in {"true", "1", "yes"}
    key = stored.get("FEED_AUTH_KEY", settings.FEED_AUTH_KEY)
    return bool(enabled), (key or None)


def active_key(settings: Settings) -> str | None:
    """The enforced key, or ``None`` when auth is off or no key is stored.

    URL builders and the route guard both call this, so serving reverts to
    keyless the instant the toggle flips off -- the stored key is retained for
    re-enable, but no URL carries it and the guard stops requiring it.
    """

    if not settings.FEED_AUTH_ENABLED:
        return None
    return settings.FEED_AUTH_KEY or None


def split_cover_token(token: str | None) -> tuple[str | None, str | None]:
    """Split a cover-art path stem into ``(episode_id, key)``.

    The stem is ``<episode_id>-<key>`` or a keyless ``<episode_id>``. The key
    (when present) is the final hyphen-separated segment and fullmatches
    ``KEY_RE``; episode ids are hyphen-free hex, so the split is unambiguous.
    Returns ``(None, None)`` for an empty token and ``(episode_id, None)`` when
    there is no valid key suffix. The guard reads the key half, the media handler
    reads the id half -- two views of the one parse.
    """

    if not token:
        return None, None
    head, sep, tail = token.rpartition("-")
    if sep and KEY_RE.fullmatch(tail):
        return head, tail
    return token, None


def verify(supplied: str | None, expected: str | None) -> bool:
    """Constant-time key check.

    The ``KEY_RE`` prefilter is load-bearing: ``hmac.compare_digest`` raises
    ``TypeError`` on non-ASCII input, which would turn a garbage ``?key=`` into
    a 500 instead of the intended 401. Anything not 64-hex can never match a
    stored key anyway.
    """

    return bool(
        expected
        and supplied
        and KEY_RE.fullmatch(supplied)
        and hmac.compare_digest(supplied, expected)
    )
