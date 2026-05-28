"""Shared atomic-write helper.

Both ``services/prompt.py`` and ``services/corrections.py`` need the same
crash-safe write sequence: temp file in the same dir, fsync the file, replace,
fsync the parent directory. Centralized here so any future fix lands once.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path


def write_bytes_atomic(path: Path, data: bytes, *, prefix: str = ".tmp-") -> None:
    """Write ``data`` to ``path`` atomically.

    - tempfile in ``path.parent`` so the final replace stays on the same FS
    - fsync the file before replace
    - replace
    - fsync the parent directory so the rename survives a kernel crash
    - clean up the temp on any failure
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        _fsync_dir(path.parent)
    except Exception:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


def _fsync_dir(directory: Path) -> None:
    """fsync the directory entry so the rename is durable across crashes.

    Best-effort: on platforms or filesystems that don't support directory
    fsync we accept the loss rather than fail the write.
    """

    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        with suppress(OSError):
            os.fsync(dir_fd)
    finally:
        with suppress(OSError):
            os.close(dir_fd)
