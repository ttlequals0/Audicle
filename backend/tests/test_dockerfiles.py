"""Images carry no curl: it drags in the Kerberos client libraries, so every
HEALTHCHECK probes /health/live with Python's stdlib instead."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILES = ("Dockerfile", "render/Dockerfile", "tts-wrapper/Dockerfile", "tts-wrapper/Dockerfile.cpu")


def _instructions(name: str) -> list[str]:
    lines = (_ROOT / name).read_text().splitlines()
    text = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    return [line for line in text.replace("\\\n", " ").splitlines() if line.strip()]


@pytest.mark.parametrize("name", _DOCKERFILES)
def test_image_installs_no_curl_and_healthchecks_with_python(name: str) -> None:
    instructions = _instructions(name)
    assert not [i for i in instructions if re.search(r"\bcurl\b", i)], f"{name} still uses curl"
    healthchecks = [i for i in instructions if i.startswith("HEALTHCHECK")]
    assert len(healthchecks) == 1, name
    assert "CMD " in healthchecks[0] and "python -I -c" in healthchecks[0], name
    assert "urllib.request" in healthchecks[0] and "/health/live" in healthchecks[0], name


def test_render_healthcheck_keeps_the_privilege_drop() -> None:
    (healthcheck,) = [i for i in _instructions("render/Dockerfile") if i.startswith("HEALTHCHECK")]
    assert "setpriv --reuid=1001" in healthcheck
    assert healthcheck.index("setpriv") < healthcheck.index("python -I -c")
