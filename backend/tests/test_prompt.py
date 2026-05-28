from __future__ import annotations

from pathlib import Path

import pytest
from app.services import prompt as prompt_service


def test_load_returns_file_contents(tmp_path: Path) -> None:
    target = tmp_path / "script.txt"
    target.write_text("rules here\nmore rules", encoding="utf-8")
    assert prompt_service.load(target) == "rules here\nmore rules"


def test_save_atomic_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "script.txt"
    prompt_service.save(target, "new prompt body", max_bytes=10240)
    assert prompt_service.load(target) == "new prompt body"


def test_save_rejects_oversize(tmp_path: Path) -> None:
    target = tmp_path / "script.txt"
    with pytest.raises(prompt_service.PromptTooLargeError):
        prompt_service.save(target, "x" * 100, max_bytes=50)


def test_save_byte_length_not_char_length(tmp_path: Path) -> None:
    """A short character string can still exceed the byte cap when encoded
    in multi-byte UTF-8."""

    target = tmp_path / "script.txt"
    # Each emoji is 4 bytes UTF-8; 20 of them = 80 bytes.
    body = "\N{ROCKET}" * 20
    with pytest.raises(prompt_service.PromptTooLargeError):
        prompt_service.save(target, body, max_bytes=50)


def test_save_atomic_no_partial_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "script.txt"
    prompt_service.save(target, "original", max_bytes=10240)

    import os

    def _boom(*_args, **_kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        prompt_service.save(target, "new content", max_bytes=10240)

    assert prompt_service.load(target) == "original"
    assert not list(tmp_path.glob(".script-*.tmp"))
