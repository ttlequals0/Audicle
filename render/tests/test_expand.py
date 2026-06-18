from __future__ import annotations

from renderer import (
    CAPTCHA_BODY_FLOOR,
    expandable_targets,
    is_captcha_wall,
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


def test_is_captcha_wall_detects_short_gate() -> None:
    assert is_captcha_wall("Verification Required. We detected unusual activity from your device.")


def test_is_captcha_wall_ignores_long_article_mentioning_marker() -> None:
    body = "This article discusses how a verification required gate works. " + ("word " * 200)
    assert len(body) >= CAPTCHA_BODY_FLOOR
    assert not is_captcha_wall(body)


def test_is_captcha_wall_ignores_short_real_text() -> None:
    assert not is_captcha_wall("A short blurb with no gate copy at all.")


def test_is_captcha_wall_detects_iframe_challenge_with_empty_body() -> None:
    # DataDome serves the wall in an iframe: the visible body is empty, so the only
    # signal is the challenge host in the page HTML. This is the retry trigger.
    html = '<iframe src="https://geo.captcha-delivery.com/captcha/?cid=x"></iframe>'
    assert is_captcha_wall("", html)


def test_is_captcha_wall_ignores_full_page_loading_challenge_script() -> None:
    # A real article whose page also loads a DataDome script is not a wall: its visible
    # body is well above the floor, so the HTML marker must not trip it (no wasted retry).
    body = "Full article body. " + ("word " * 200)
    html = '<script src="https://ct.captcha-delivery.com/c.js"></script>'
    assert len(body) >= CAPTCHA_BODY_FLOOR
    assert not is_captcha_wall(body, html)


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
