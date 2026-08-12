"""Memory measurement and the cleanup ladder that replaced the OOM kills."""

from __future__ import annotations

import memory


def test_rss_mb_reports_a_plausible_size() -> None:
    """The reading has to be real: the ladder's thresholds are meaningless if
    this returns a constant."""

    value = memory.rss_mb()
    assert value > 0
    # A Python process with pytest loaded is comfortably inside this range;
    # the point is to catch a unit error (bytes or pages read as MB).
    assert value < 100_000


def test_cleanup_reports_before_and_after() -> None:
    stats = memory.cleanup()
    assert stats["rss_before_mb"] > 0
    assert stats["rss_after_mb"] > 0
    assert stats["reclaimed_mb"] == stats["rss_before_mb"] - stats["rss_after_mb"]
    # malloc_trim is the point of the exercise on glibc. It is allowed to be
    # absent (musl), but the flag must say which happened rather than pretend.
    assert stats["malloc_trim"] in (0, 1)


def test_maybe_cleanup_is_a_no_op_below_the_soft_limit() -> None:
    """Only cross the threshold, never on every chunk: trimming has a cost and
    the common case is a wrapper sitting well under the limit."""

    assert memory.maybe_cleanup(soft_limit_mb=10_000_000) is None


def test_maybe_cleanup_runs_once_over_the_soft_limit() -> None:
    stats = memory.maybe_cleanup(soft_limit_mb=1)
    assert stats is not None
    assert stats["rss_before_mb"] > 0


def test_zero_soft_limit_disables_cleanup() -> None:
    assert memory.maybe_cleanup(soft_limit_mb=0) is None


def test_hard_limit_checks_and_zero_disables() -> None:
    assert memory.over_hard_limit(1) is True
    assert memory.over_hard_limit(10_000_000) is False
    # Disabled, so never trips regardless of actual usage.
    assert memory.over_hard_limit(0) is False


def test_cleanup_tolerates_a_torch_module_that_raises() -> None:
    """A torch build whose cuda probe throws must not take down a chunk that
    already synthesized successfully."""

    class Exploding:
        class cuda:  # noqa: N801 - mirrors torch's attribute layout
            @staticmethod
            def is_available():
                raise RuntimeError("driver gone")

    stats = memory.cleanup(Exploding())
    assert stats["rss_before_mb"] > 0


def test_cleanup_calls_empty_cache_when_cuda_is_available() -> None:
    calls: list[str] = []

    class FakeTorch:
        class cuda:  # noqa: N801
            @staticmethod
            def is_available():
                return True

            @staticmethod
            def empty_cache():
                calls.append("empty_cache")

    memory.cleanup(FakeTorch())
    assert calls == ["empty_cache"]


def test_cleanup_skips_empty_cache_without_cuda() -> None:
    calls: list[str] = []

    class FakeTorch:
        class cuda:  # noqa: N801
            @staticmethod
            def is_available():
                return False

            @staticmethod
            def empty_cache():
                calls.append("empty_cache")

    memory.cleanup(FakeTorch())
    assert calls == []
