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
