"""Episode retention sweep.

Daily background task that deletes episodes (and their on-disk media) older
than ``RETENTION_DAYS``. Also exposed via ``POST /api/v1/purge`` for an
operator-initiated wipe.

The retention sweep runs once per day at ``RETENTION_SWEEP_HOUR_UTC``; the
purge endpoint accepts an ``older_than_days`` override so an operator can
clear stale content without waiting for the cron-style trigger.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import Settings
from app.core import database
from app.core.paths import media_dir

logger = logging.getLogger("app.services.retention")


@dataclass(frozen=True)
class PurgeResult:
    episode_ids: tuple[str, ...]
    rows_deleted: int
    files_removed: int


# Upper bound on the day-count parameter; Python's datetime only spans years
# 1..9999, so ``datetime.now() - timedelta(days=N)`` overflows past roughly
# 2.9M days. Cap at 100k days (~273 years) which is well past any plausible
# retention window and still safely inside the datetime range.
_MAX_OLDER_THAN_DAYS = 100_000


def purge_older_than(
    settings: Settings,
    older_than_days: int,
) -> PurgeResult:
    """Delete episode rows + on-disk media older than ``older_than_days``.

    ``older_than_days=0`` is the explicit "wipe everything" contract used by
    the purge endpoint: it removes every row unconditionally (including any
    future-dated rows from clock skew or test fixtures). Positive N filters
    rows strictly older than ``now - N days``.
    """

    if older_than_days < 0 or older_than_days > _MAX_OLDER_THAN_DAYS:
        raise ValueError(
            f"older_than_days must be in [0, {_MAX_OLDER_THAN_DAYS}], got {older_than_days}"
        )

    # Sentinel cutoff for the wipe-all path collapses to a single query: any
    # real row's pub_date is strictly less than the year-9999 sentinel.
    if older_than_days == 0:
        cutoff_iso = "9999-12-31T23:59:59Z"
    else:
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = database.connect(database.db_path(settings.DATA_DIR))
    try:
        rows = conn.execute(
            """
            SELECT id, audio_path, artwork_path
            FROM episodes
            WHERE pub_date < ?
            """,
            (cutoff_iso,),
        ).fetchall()
        episode_ids = tuple(row["id"] for row in rows)
        for row in rows:
            conn.execute("DELETE FROM episodes WHERE id = ?", (row["id"],))
        conn.commit()
    finally:
        conn.close()

    files_removed = 0
    out_root = media_dir(settings)
    for row in rows:
        for path_str in (row["audio_path"], row["artwork_path"]):
            if path_str and _remove_path(Path(path_str), root_guard=out_root):
                files_removed += 1
        # The VTT lives in the DB, not on disk, so the row delete is the
        # only cleanup for it. Older code paths may have created a stub
        # .vtt under media_dir though; check for one.
        if _remove_path(out_root / f"{row['id']}.vtt", root_guard=out_root):
            files_removed += 1

    logger.info(
        "Retention sweep complete",
        extra={
            "event": "retention_sweep_complete",
            "older_than_days": older_than_days,
            "cutoff": "wipe_all" if older_than_days == 0 else cutoff_iso,
            "rows_deleted": len(rows),
            "files_removed": files_removed,
        },
    )
    return PurgeResult(
        episode_ids=episode_ids,
        rows_deleted=len(rows),
        files_removed=files_removed,
    )


def _remove_path(path: Path, *, root_guard: Path | None = None) -> bool:
    """Unlink ``path``. If ``root_guard`` is provided, refuse paths that
    don't resolve under it (defense-in-depth against a poisoned row pointing
    at ``/etc/passwd``). Missing-file is silently treated as success (the
    sweep is idempotent). All other ``OSError`` cases (e.g.
    ``IsADirectoryError``, ``PermissionError``) log a WARNING so operators
    can grep for stuck artifacts after a sweep.
    """

    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        logger.warning(
            "Could not resolve path during retention sweep",
            extra={
                "event": "retention_resolve_failed",
                "path": str(path),
                "error_class": type(exc).__name__,
            },
        )
        return False
    if root_guard is not None:
        try:
            resolved.relative_to(root_guard.resolve())
        except ValueError:
            logger.warning(
                "Refusing to remove path outside DATA_DIR/media",
                extra={
                    "event": "retention_unsafe_path",
                    "path": str(path),
                },
            )
            return False
    if not resolved.exists():
        return False
    try:
        resolved.unlink()
    except OSError as exc:
        logger.warning(
            "Failed to unlink path during retention sweep",
            extra={
                "event": "retention_unlink_failed",
                "path": str(resolved),
                "error_class": type(exc).__name__,
            },
        )
        return False
    return True
