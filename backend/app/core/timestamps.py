"""ISO-8601 timestamp parsing helper.

SQLite's ``strftime('%Y-%m-%dT%H:%M:%SZ', 'now')`` produces strings with a
literal ``Z`` suffix. ``datetime.fromisoformat`` accepts ``Z`` natively on
Python 3.11+, but the explicit ``Z`` -> ``+00:00`` swap keeps the helper
robust against a future migration that emits offsets without ``Z`` and
makes the timezone-naive path impossible.
"""

from __future__ import annotations

from datetime import UTC, datetime


def parse_iso(value: str | None) -> datetime | None:
    """Parse ``value`` as ISO-8601 -> UTC-aware datetime, or ``None`` on
    parse failure / missing input."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
