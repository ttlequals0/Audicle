from __future__ import annotations

from app.services import csrf


def test_issue_token_is_url_safe_and_unique() -> None:
    a = csrf.issue_token()
    b = csrf.issue_token()
    assert a != b
    # URL-safe characters only.
    assert all(c.isalnum() or c in "-_" for c in a)
    # Long enough that brute force is impractical.
    assert len(a) >= 32


def test_verify_token_matches_on_equal_strings() -> None:
    token = csrf.issue_token()
    assert csrf.verify_token(token, token) is True


def test_verify_token_rejects_mismatch() -> None:
    assert csrf.verify_token("a", "b") is False


def test_verify_token_rejects_missing_header() -> None:
    assert csrf.verify_token(None, "cookie") is False


def test_verify_token_rejects_missing_cookie() -> None:
    assert csrf.verify_token("header", None) is False


def test_verify_token_rejects_empty_strings() -> None:
    assert csrf.verify_token("", "") is False
