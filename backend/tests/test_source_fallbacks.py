from __future__ import annotations

from app.services import source_fallbacks


def test_match_medium_exact_and_subdomain() -> None:
    assert source_fallbacks.match("https://medium.com/p/abc").name == "medium-freedium"
    assert source_fallbacks.match("https://wesbrown18.medium.com/the-post-abc").name == (
        "medium-freedium"
    )


def test_match_returns_none_for_unlisted_host() -> None:
    assert source_fallbacks.match("https://example.com/article") is None
    # A host merely containing the suffix as a substring must not match.
    assert source_fallbacks.match("https://notmedium.com.evil.test/x") is None


def test_candidate_urls_fill_template_in_order() -> None:
    rule = source_fallbacks.match("https://wesbrown18.medium.com/post-abc")
    assert rule is not None
    url = "https://wesbrown18.medium.com/post-abc"
    candidates = source_fallbacks.candidate_urls(rule, url)
    assert candidates == [
        ("medium-freedium#0", f"https://freedium.cfd/{url}"),
        ("medium-freedium#1", f"https://freedium-mirror.cfd/{url}"),
    ]


def test_medium_rule_bar_above_global_floor() -> None:
    # The teaser (~1.5 KB) clears the 500-char global floor, so the rule must set a
    # higher bar to detect it.
    rule = source_fallbacks.match("https://medium.com/p/abc")
    assert rule is not None
    assert rule.min_chars > 500
