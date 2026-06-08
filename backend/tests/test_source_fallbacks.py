from __future__ import annotations

from app.services import source_fallbacks as sf


def test_match_medium_builtin_exact_and_subdomain() -> None:
    assert sf.match("https://medium.com/p/abc").name == "medium"
    assert sf.match("https://wesbrown18.medium.com/post-abc").name == "medium"


def test_match_none_for_unlisted_and_substring_host() -> None:
    assert sf.match("https://example.com/article") is None
    # A host merely containing the suffix as a substring must not match.
    assert sf.match("https://notmedium.com.evil.test/x") is None


def test_builtin_medium_uses_freedium_above_global_floor() -> None:
    rule = sf.match("https://medium.com/p/abc")
    assert rule is not None
    assert rule.proxy == "freedium"
    assert rule.min_chars > 500


def test_flaresolverr_is_an_offered_strategy_with_no_candidate_attempts() -> None:
    # flaresolverr is a valid strategy, but it is driven by the extractor (a POST
    # to the solver), not by a Firecrawl target URL -- so it yields no candidates.
    assert "flaresolverr" in sf.PROXY_KEYS
    rule = sf.SourceFallback("op", ("gated.test",), "flaresolverr", "", 3000)
    assert sf.candidate_attempts(rule, "https://gated.test/post") == []


def test_candidate_attempts_googlebot_rescrapes_same_url_with_headers() -> None:
    rule = sf.SourceFallback("wapo", ("washingtonpost.com",), "googlebot", "", 3000)
    url = "https://www.washingtonpost.com/a"
    attempts = sf.candidate_attempts(rule, url)
    assert len(attempts) == 1
    _, target, headers = attempts[0]
    assert target == url  # same URL, not a rewrite
    assert "googlebot" in headers["User-Agent"].lower()
    assert headers["X-Forwarded-For"] == "66.249.66.1"


def test_candidate_attempts_freedium_two_rewrites_no_headers() -> None:
    rule = sf.SourceFallback("medium", ("medium.com",), "freedium", "", 3000)
    url = "https://medium.com/p/abc"
    attempts = sf.candidate_attempts(rule, url)
    assert [(t, h) for _, t, h in attempts] == [
        (f"https://freedium.cfd/{url}", {}),
        (f"https://freedium-mirror.cfd/{url}", {}),
    ]


def test_candidate_attempts_custom_template() -> None:
    rule = sf.SourceFallback("x", ("x.com",), "custom", "https://rd.example/{url}", 3000)
    attempts = sf.candidate_attempts(rule, "https://x.com/a")
    assert attempts == [("x#custom", "https://rd.example/https://x.com/a", {})]


def test_candidate_attempts_none_is_empty() -> None:
    rule = sf.SourceFallback("wsj", ("wsj.com",), "none", "", 3000)
    assert sf.candidate_attempts(rule, "https://wsj.com/a") == []


def test_build_registry_operator_overrides_builtin_and_uses_default_proxy() -> None:
    rules = [
        {"host": "washingtonpost.com"},  # no proxy -> default
        {"host": "medium.com", "proxy": "googlebot"},  # override builtin
    ]
    reg = sf.build_registry(rules, default_proxy="googlebot", min_chars=4000)
    medium = sf.match("https://medium.com/p/x", reg)
    assert medium is not None and medium.proxy == "googlebot"  # operator wins over builtin
    wapo = sf.match("https://www.washingtonpost.com/a", reg)
    assert wapo is not None and wapo.proxy == "googlebot" and wapo.min_chars == 4000
    assert sf.match("https://example.com/x", reg) is None
