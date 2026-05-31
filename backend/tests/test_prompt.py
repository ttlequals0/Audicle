from __future__ import annotations

from pathlib import Path

import pytest
from app.core import database
from app.services import prompt as prompt_service


def _conn(env: Path):
    database.run_migrations(env)
    return database.connect(database.db_path(env))


def test_load_effective_returns_packaged_default_when_unset(env: Path) -> None:
    conn = _conn(env)
    try:
        # No override stored -> the shipped default file content, and is_default True.
        assert prompt_service.load_effective(conn, "cleanup") == prompt_service.default_text("cleanup")
        assert prompt_service.is_default(conn, "cleanup") is True
    finally:
        conn.close()


def test_save_override_then_load_effective(env: Path) -> None:
    conn = _conn(env)
    try:
        prompt_service.save_override(conn, "cleanup", "new prompt body", max_bytes=10240)
        assert prompt_service.load_effective(conn, "cleanup") == "new prompt body"
        assert prompt_service.is_default(conn, "cleanup") is False
    finally:
        conn.close()


def test_reset_restores_default(env: Path) -> None:
    conn = _conn(env)
    try:
        prompt_service.save_override(conn, "cleanup", "custom", max_bytes=10240)
        prompt_service.reset(conn, "cleanup")
        assert prompt_service.is_default(conn, "cleanup") is True
        assert prompt_service.load_effective(conn, "cleanup") == prompt_service.default_text("cleanup")
    finally:
        conn.close()


def test_summary_kind_uses_its_own_default(env: Path) -> None:
    conn = _conn(env)
    try:
        assert prompt_service.load_effective(conn, "summary") == prompt_service.default_text("summary")
        # Overriding cleanup must not affect summary.
        prompt_service.save_override(conn, "cleanup", "only cleanup", max_bytes=10240)
        assert prompt_service.is_default(conn, "summary") is True
    finally:
        conn.close()


def test_pronunciation_kind_uses_its_own_default(env: Path) -> None:
    conn = _conn(env)
    try:
        default = prompt_service.default_text("pronunciation")
        assert default.strip()  # the packaged file is non-empty
        assert prompt_service.load_effective(conn, "pronunciation") == default
        assert prompt_service.is_default(conn, "pronunciation") is True
        # Overriding it is independent of cleanup/summary.
        prompt_service.save_override(conn, "pronunciation", "only pronunciation", max_bytes=10240)
        assert prompt_service.load_effective(conn, "pronunciation") == "only pronunciation"
        assert prompt_service.is_default(conn, "cleanup") is True
    finally:
        conn.close()


def test_save_rejects_oversize(env: Path) -> None:
    conn = _conn(env)
    try:
        with pytest.raises(prompt_service.PromptTooLargeError):
            prompt_service.save_override(conn, "cleanup", "x" * 100, max_bytes=50)
    finally:
        conn.close()


def test_save_byte_length_not_char_length(env: Path) -> None:
    """A short character string can still exceed the byte cap in multi-byte UTF-8."""

    conn = _conn(env)
    try:
        body = "\N{ROCKET}" * 20  # 4 bytes each = 80 bytes
        with pytest.raises(prompt_service.PromptTooLargeError):
            prompt_service.save_override(conn, "cleanup", body, max_bytes=50)
    finally:
        conn.close()
