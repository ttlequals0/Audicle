from __future__ import annotations

from renderer import (
    CAPTCHA_BODY_FLOOR,
    expandable_targets,
    is_captcha_gate,
    is_public_url,
    word_estimate,
)


def test_expandable_targets_finds_controls_and_ignores_prose() -> None:
    controls = [
        "EXPAND TO CONTINUE READING",
        "Sign in",
        "Read more",
        "Subscribe",
        "Show more",
        "Home",
        "See more",
        "Load more",
        "View more",
    ]
    assert expandable_targets(controls) == [0, 2, 4, 6, 7, 8]


def test_expandable_targets_normalizes_whitespace() -> None:
    assert expandable_targets(["CONTINUE\n  READING"]) == [0]


def test_expandable_targets_does_not_match_continued() -> None:
    # "continued reading" (past tense, in body prose) must not look like a control.
    assert expandable_targets(["She continued reading the report"]) == []


def test_expandable_targets_empty() -> None:
    assert expandable_targets(["Next", "Previous", "Comments"]) == []


def test_is_captcha_gate_detects_short_gate() -> None:
    assert is_captcha_gate("Verification Required. We detected unusual activity from your device.")


def test_is_captcha_gate_ignores_long_article_mentioning_marker() -> None:
    body = "This article discusses how a verification required gate works. " + ("word " * 200)
    assert len(body) >= CAPTCHA_BODY_FLOOR
    assert not is_captcha_gate(body)


def test_is_captcha_gate_ignores_short_real_text() -> None:
    assert not is_captcha_gate("A short blurb with no gate copy at all.")


def test_word_estimate() -> None:
    assert word_estimate("one two three") == 3
    assert word_estimate("") == 0


def test_is_public_url_rejects_private_and_loopback() -> None:
    assert not is_public_url("http://127.0.0.1/x")
    assert not is_public_url("http://localhost/x")
    assert not is_public_url("http://10.0.0.5/x")
    assert not is_public_url("http://192.168.1.10/x")
    assert not is_public_url("https://[::1]/x")
    assert not is_public_url("not a url")


def test_is_public_url_allows_public_literal() -> None:
    # A public literal IP resolves offline (no DNS), so this is deterministic.
    assert is_public_url("https://93.184.216.34/article")
