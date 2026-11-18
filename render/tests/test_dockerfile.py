"""Render-only Dockerfile invariants."""

from __future__ import annotations

from pathlib import Path

_DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"


def test_healthcheck_keeps_the_privilege_drop_before_python() -> None:
    lines = [line for line in _DOCKERFILE.read_text().splitlines() if not line.lstrip().startswith("#")]
    text = "\n".join(lines).replace("\\\n", " ")
    (healthcheck,) = [line for line in text.splitlines() if line.startswith("HEALTHCHECK")]
    assert "setpriv --reuid=1001" in healthcheck
    assert healthcheck.index("setpriv") < healthcheck.index("python -I -c")


def test_cups_stub_step_fails_the_build_if_the_freed_libraries_survive() -> None:
    text = _DOCKERFILE.read_text()
    assert "survived the CUPS removal" in text
    assert "libcups files remain" in text
    for package in ("libgssapi-krb5-2", "libkrb5-3", "libavahi-client3", "libgnutls30t64"):
        assert package in text
