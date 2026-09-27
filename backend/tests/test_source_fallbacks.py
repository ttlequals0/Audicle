from __future__ import annotations

from app.config import Settings
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


def test_flaresolverr_is_a_selectable_per_host_attempt() -> None:
    # FlareSolverr is selectable per-host (for hosts that hard-block the scraper IP);
    # candidate_attempts yields one flaresolverr-engine Attempt (a browser fetch), not
    # a Firecrawl re-scrape.
    assert "flaresolverr" in sf.PROXY_KEYS
    rule = sf.SourceFallback("operator:nytimes.com", ("nytimes.com",), "flaresolverr", "", 500)
    attempts = sf.candidate_attempts(rule, "https://www.nytimes.com/a")
    assert len(attempts) == 1
    assert attempts[0].engine == "flaresolverr"
    assert attempts[0].url == "https://www.nytimes.com/a"


def test_candidate_attempts_googlebot_rescrapes_same_url_with_headers() -> None:
    rule = sf.SourceFallback("wapo", ("washingtonpost.com",), "googlebot", "", 3000)
    url = "https://www.washingtonpost.com/a"
    attempts = sf.candidate_attempts(rule, url)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.engine == "refetch"
    assert attempt.url == url  # same URL, not a rewrite
    assert "googlebot" in attempt.headers["User-Agent"].lower()
    assert attempt.headers["X-Forwarded-For"] == "66.249.66.1"


def test_candidate_attempts_freedium_two_rewrites_no_headers() -> None:
    rule = sf.SourceFallback("medium", ("medium.com",), "freedium", "", 3000)
    url = "https://medium.com/p/abc"
    attempts = sf.candidate_attempts(rule, url)
    assert all(a.engine == "firecrawl" and a.headers == {} for a in attempts)
    assert [a.url for a in attempts] == [
        f"https://freedium.cfd/{url}",
        f"https://freedium-mirror.cfd/{url}",
    ]


def test_candidate_attempts_custom_template() -> None:
    rule = sf.SourceFallback("x", ("x.com",), "custom", "https://rd.example/{url}", 3000)
    attempts = sf.candidate_attempts(rule, "https://x.com/a")
    assert attempts == [
        sf.Attempt("x#custom", "firecrawl", "https://rd.example/https://x.com/a", is_host_rule=True)
    ]


def test_candidate_attempts_none_is_empty() -> None:
    rule = sf.SourceFallback("wsj", ("wsj.com",), "none", "", 3000)
    assert sf.candidate_attempts(rule, "https://wsj.com/a") == []


def test_candidate_attempts_archive() -> None:
    rule = sf.SourceFallback("op", ("x.com",), "archive", "", 3000)
    assert sf.candidate_attempts(rule, "https://x.com/a") == [
        sf.Attempt("host-rule#archive", "archive", "https://x.com/a", is_host_rule=True)
    ]


def test_candidate_attempts_reader_passes_article_url_to_reader_engine() -> None:
    # The reader engine applies the proxy template itself, so the attempt carries the
    # original article URL (the engine tag routes it to reader.fetch).
    rule = sf.SourceFallback("wsj", ("wsj.com",), "reader", "", 3000)
    assert sf.candidate_attempts(rule, "https://www.wsj.com/a") == [
        sf.Attempt("wsj#reader", "reader", "https://www.wsj.com/a", is_host_rule=True)
    ]


def test_build_registry_global_default_catch_all_applies_to_any_host() -> None:
    # With a global_floor and a real default_proxy, build_registry appends a
    # lowest-priority catch-all so the default proxy matches any host, at the hard
    # MIN_EXTRACTION_CHARS (near-empty) trigger floor.
    reg = sf.build_registry([], "googlebot", 3000, global_floor=500)
    rule = sf.match("https://unlisted.test/a", reg)
    assert rule is not None and rule.catch_all
    assert rule.proxy == "googlebot"
    assert rule.min_chars == 500
    attempt = sf.candidate_attempts(rule, "https://unlisted.test/a")[0]
    assert attempt.url == "https://unlisted.test/a"  # googlebot re-scrapes the same url
    assert "googlebot" in attempt.headers["User-Agent"].lower()


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
        {"host": "example.org"},  # no builtin, no proxy -> default
        {"host": "medium.com", "proxy": "googlebot"},  # override builtin
    ]
    reg = sf.build_registry(rules, default_proxy="googlebot", min_chars=4000)
    medium = sf.match("https://medium.com/p/x", reg)
    assert medium is not None and medium.proxy == "googlebot"  # operator wins over builtin
    org = sf.match("https://example.org/a", reg)
    assert org is not None and org.proxy == "googlebot" and org.min_chars == 4000
    assert sf.match("https://example.net/x", reg) is None


def test_cookie_only_operator_rule_keeps_builtin_render_strategy() -> None:
    # An operator storing a cookie jar for a built-in render host must not have to
    # re-pick the strategy: an empty proxy inherits the builtin's render rung so the
    # jar rides along by default.
    rules = [{"host": "wsj.com", "cookies": "sid=1"}]
    reg = sf.build_registry(rules, default_proxy="googlebot", min_chars=3000, global_floor=150)
    rule = sf.match("https://www.wsj.com/a", reg)
    assert rule is not None
    assert rule.proxy == "render" and rule.cookies == "sid=1"
    attempts = sf.candidate_attempts(rule, "https://www.wsj.com/a")
    assert [(a.engine, a.cookies) for a in attempts] == [("render", "sid=1")]


def test_render_rule_emits_a_render_attempt_with_its_cookies() -> None:
    assert "render" in sf.PROXY_KEYS
    rule = sf.SourceFallback("operator:wsj.com", ("wsj.com",), "render", "", 0, cookies="sid=1")
    attempts = sf.candidate_attempts(rule, "https://www.wsj.com/a")
    assert [(a.engine, a.url, a.cookies, a.is_host_rule) for a in attempts] == [
        ("render", "https://www.wsj.com/a", "sid=1", True)
    ]


def test_shipped_defaults_match_tuned_live() -> None:
    # Defaults a fresh deploy gets without any Settings-API tuning.
    assert Settings().MIN_EXTRACTION_CHARS == 150
    for host in (
        "inc.com",
        "wsj.com",
        "www.wsj.com",
        "nytimes.com",
        "www.theatlantic.com",
        "bostonglobe.com",
    ):
        rule = sf.match(f"https://{host}/a")
        assert rule is not None
        assert rule.proxy == "render"
        assert rule.min_chars == 0


def test_builtin_reader_rule_ships_for_ft_com() -> None:
    rule = sf.match("https://www.ft.com/content/abc")
    assert rule is not None
    assert rule.proxy == "reader"
    # FT's promo-only block measures ~1.1k chars, so the teaser floor must sit above it.
    assert rule.min_chars == 1500


def test_operator_rule_overrides_builtin_render() -> None:
    reg = sf.build_registry([{"host": "inc.com", "proxy": "flaresolverr"}], "googlebot", 3000, 500)
    rule = sf.match("https://www.inc.com/a", reg)
    assert rule is not None and rule.proxy == "flaresolverr"
