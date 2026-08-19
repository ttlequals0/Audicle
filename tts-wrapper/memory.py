"""Resident-memory measurement and cleanup for the long-lived wrapper process.

The wrapper serves hundreds of chunks from one process. Before 0.55.0 it did no
per-chunk cleanup at all, resident memory climbed to roughly 20 GB over a long
job, and the host OOM killer took it out mid-inference. The kills were strikingly
consistent (20.12, 20.19, 20.26 GB), which points at allocator retention rather
than an unbounded object leak: glibc's ``free()`` returns memory to per-arena
free lists and does not hand pages back to the kernel, so a process making large
repeated numpy/torch allocations shows monotonically rising RSS with nothing
actually leaked.

``malloc_trim`` is the call that releases those arenas. It is the reason this
module exists; ``gc.collect()`` and ``torch.cuda.empty_cache()`` are cheap
companions rather than the main event, and ``empty_cache`` in particular frees
VRAM, which is not what was exhausted.

Every cleanup logs the reclaimed delta. That is deliberate: if trimming reclaims
nothing, the arena theory is wrong and the per-chunk RSS series shows where the
growth really is.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import gc
import logging
import os
import threading

logger = logging.getLogger("tts.memory")

_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
_BYTES_PER_MB = 1024 * 1024

# Resolved once. None when unavailable (non-glibc, e.g. musl), in which case the
# trim step is skipped and the other two still run.
_libc: ctypes.CDLL | None = None
_libc_resolved = False
_libc_lock = threading.Lock()


def _malloc_trim() -> bool:
    """Return freed glibc arena pages to the kernel. True when the call ran."""

    global _libc, _libc_resolved
    with _libc_lock:
        if not _libc_resolved:
            _libc_resolved = True
            try:
                name = ctypes.util.find_library("c")
                candidate = ctypes.CDLL(name) if name else None
                # musl provides libc but no malloc_trim; probe before trusting it.
                if candidate is not None and hasattr(candidate, "malloc_trim"):
                    candidate.malloc_trim.argtypes = [ctypes.c_size_t]
                    candidate.malloc_trim.restype = ctypes.c_int
                    _libc = candidate
            except OSError:
                _libc = None
    if _libc is None:
        return False
    try:
        _libc.malloc_trim(0)
        return True
    except OSError:
        return False


def rss_mb() -> int:
    """Resident set size in MB, or 0 where /proc is unavailable.

    Reads field 2 of /proc/self/statm (resident pages). Cheap enough to call
    after every chunk, and it is the same number the kernel's OOM report prints
    as ``anon-rss``, so logs here line up with a kill in the kernel log.
    """

    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            resident_pages = int(handle.read().split()[1])
    except (OSError, IndexError, ValueError):
        return 0
    return resident_pages * _PAGE_SIZE // _BYTES_PER_MB


def cleanup(torch_module: object | None = None) -> dict[str, int]:
    """Run the cleanup ladder and report what it reclaimed.

    Returns the before/after RSS and the delta so a caller can log one record
    and an operator can see whether trimming is doing anything.
    """

    before = rss_mb()
    collected = gc.collect()
    if torch_module is not None:
        try:
            if torch_module.cuda.is_available():  # type: ignore[attr-defined]
                torch_module.cuda.empty_cache()  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - torch internals vary by build
            logger.debug("empty_cache failed", exc_info=True)
    trimmed = _malloc_trim()
    after = rss_mb()
    return {
        "rss_before_mb": before,
        "rss_after_mb": after,
        "reclaimed_mb": before - after,
        "gc_objects": collected,
        "malloc_trim": int(trimmed),
    }


def maybe_cleanup(
    soft_limit_mb: int,
    torch_module: object | None = None,
) -> dict[str, int] | None:
    """Clean up when RSS is above ``soft_limit_mb``. None when below, or when the
    limit is zero (disabled), so the caller can skip logging entirely."""

    if soft_limit_mb <= 0:
        return None
    current = rss_mb()
    if current < soft_limit_mb:
        return None
    stats = cleanup(torch_module)
    logger.info(
        "Memory cleanup",
        extra={
            "event": "tts_memory_cleanup",
            "soft_limit_mb": soft_limit_mb,
            **stats,
        },
    )
    return stats


def over_soft_limit(soft_limit_mb: int, current_mb: int) -> bool:
    """True when ``current_mb`` RSS is above the soft ceiling. Zero disables
    the check, the same convention as ``maybe_cleanup``/``over_hard_limit``."""

    return soft_limit_mb > 0 and current_mb > soft_limit_mb


def over_hard_limit(hard_limit_mb: int) -> bool:
    """True when RSS is still above the hard ceiling after a cleanup, meaning a
    restart is the remaining option. Zero disables the check."""

    return hard_limit_mb > 0 and rss_mb() >= hard_limit_mb
