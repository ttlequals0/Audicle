"""Camoufox is not installed in the test environment (the sidecar imports it lazily so
the pure helpers stay testable), so a stub module stands in for it."""

import sys
import types

_camoufox = types.ModuleType("camoufox")
_async_api = types.ModuleType("camoufox.async_api")
_async_api.AsyncCamoufox = object
sys.modules.setdefault("camoufox", _camoufox)
sys.modules.setdefault("camoufox.async_api", _async_api)

import pytest  # noqa: E402

import camoufox_renderer  # noqa: E402


@pytest.fixture(autouse=True)
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each clock read advances 100 ms, so the renderer's poll loops end fast and alike."""

    ticks = iter(range(10**9))
    monkeypatch.setattr(
        camoufox_renderer, "time", types.SimpleNamespace(monotonic=lambda: next(ticks) / 10)
    )
