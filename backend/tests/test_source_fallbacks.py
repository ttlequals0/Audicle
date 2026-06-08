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


def test_flaresolverr_is_a_selectable_strategy_with_no_firecrawl_attempts() -> None:
    # FlareSolverr is selectable per-host (for hosts that hard-block the scraper IP),
    # but extract() handles it via the solver -- candidate_attempts yields nothing,
    # so it never produces a (mis-routed) Firecrawl re-scrape.
    assert "flaresolverr" in sf.PROXY_KEYS
    rule = sf.SourceFallback("operator:nytimes.com", ("nytimes.com",), "flaresolverr", "", 500)
    assert sf.candidate_attempts(rule, "https://www.nytimes.com/a") == []


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


def test_build_registry_global_default_catch_all_applies_to_any_host() -> None:
    # With a global_floor and a real default_proxy, build_registry appends a
    # lowest-priority catch-all so the default proxy matches any host, at the hard
    # MIN_EXTRACTION_CHARS (near-empty) trigger floor.
    reg = sf.build_registry([], "googlebot", 3000, global_floor=500)
    rule = sf.match("https://unlisted.test/a", reg)
    assert rule is not None and rule.catch_all
    assert rule.proxy == "googlebot"
    assert rule.min_chars == 500
    _, target, headers = sf.candidate_attempts(rule, "https://unlisted.test/a")[0]
    assert target == "https://unlisted.test/a"  # googlebot re-scrapes the same url
    assert "googlebot" in headers["User-Agent"].lower()


def test_build_registry_per_host_rule_overrides_global_catch_all() -> None:
    reg = sf.build_registry([{"host": "medium.com", "proxy": "freedium"}], "googlebot", 3000, 500)
    rule = sf.match("https://medium.com/p/x", reg)
    assert rule is not None and not rule.catch_all
    # The per-host rule wins and keeps its higher teaser floor, not the global one.
    assert rule.proxy == "freedium" and rule.min_chars == 3000


def test_build_registry_no_catch_all_without_global_default() -> None:
    # default_proxy "none"/"" or global_floor 0 -> no catch-all (plain behavior).
    assert sf.match("https://unlisted.test/a", sf.build_registry([], "none", 3000, 500)) is None
    assert sf.match("https://unlisted.test/a", sf.build_registry([], "", 3000, 500)) is None
    assert sf.match("https://unlisted.test/a", sf.build_registry([], "googlebot", 3000)) is None


def test_build_registry_per_host_none_opts_out_of_global_catch_all() -> None:
    reg = sf.build_registry([{"host": "wsj.com", "proxy": "none"}], "googlebot", 3000, 500)
    rule = sf.match("https://www.wsj.com/a", reg)
    assert rule is not None and rule.proxy == "none" and not rule.catch_all
    assert sf.candidate_attempts(rule, "https://www.wsj.com/a") == []


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
