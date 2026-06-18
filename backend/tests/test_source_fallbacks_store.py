from __future__ import annotations

from pathlib import Path

import pytest
from app.core import database
from app.services import source_fallbacks_store as store


def test_load_returns_defaults_when_unset(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        cfg = store.load(conn)
    assert cfg["default_proxy"] == "googlebot"
    assert cfg["min_chars"] == 3000
    assert cfg["rules"] == []


def test_save_then_load_round_trips_and_normalizes_rules(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        store.save(
            conn,
            {
                "default_proxy": "freedium",
                "min_chars": 2500,
                "rules": [{"host": "WashingtonPost.com", "proxy": "none"}],
            },
        )
        cfg = store.load(conn)
    assert cfg["default_proxy"] == "freedium"
    assert cfg["min_chars"] == 2500
    # host lowercased, custom_template and cookies defaulted
    assert cfg["rules"] == [
        {"host": "washingtonpost.com", "proxy": "none", "custom_template": "", "cookies": ""}
    ]


def test_save_rejects_bad_proxy(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn, pytest.raises(ValueError):
        store.save(conn, {"default_proxy": "bogus", "min_chars": 3000, "rules": []})


def test_save_rejects_custom_without_url_placeholder(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn, pytest.raises(ValueError):
        store.save(
            conn,
            {
                "default_proxy": "googlebot",
                "min_chars": 3000,
                "rules": [{"host": "x.com", "proxy": "custom", "custom_template": "https://nope/"}],
            },
        )


def test_save_rejects_empty_host(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn, pytest.raises(ValueError):
        store.save(
            conn,
            {"default_proxy": "googlebot", "min_chars": 3000, "rules": [{"host": "  "}]},
        )


def test_save_rejects_default_proxy_custom(env: Path) -> None:
    # No global template field exists, so a custom default could never render.
    database.run_migrations(env)
    with database.connection(env) as conn, pytest.raises(ValueError):
        store.save(conn, {"default_proxy": "custom", "min_chars": 3000, "rules": []})


def test_save_rejects_default_proxy_render(env: Path) -> None:
    # render drives a headful browser per page; as a global default it would render
    # every article. It is a per-host strategy only.
    database.run_migrations(env)
    with database.connection(env) as conn, pytest.raises(ValueError):
        store.save(conn, {"default_proxy": "render", "min_chars": 3000, "rules": []})


def test_save_accepts_render_per_host_rule(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        store.save(
            conn,
            {
                "default_proxy": "googlebot",
                "min_chars": 3000,
                "rules": [{"host": "inc.com", "proxy": "render"}],
            },
        )
        loaded = store.load(conn)
    assert loaded["rules"][0]["host"] == "inc.com"
    assert loaded["rules"][0]["proxy"] == "render"


def test_save_rejects_bool_min_chars(env: Path) -> None:
    # bool is an int subclass; True would silently coerce to 1 and disable detection.
    database.run_migrations(env)
    with database.connection(env) as conn, pytest.raises(ValueError):
        store.save(conn, {"default_proxy": "googlebot", "min_chars": True, "rules": []})


def test_save_rejects_host_with_scheme_or_path(env: Path) -> None:
    # An operator pasting a full article URL must be rejected, not stored as a dead rule.
    database.run_migrations(env)
    with database.connection(env) as conn, pytest.raises(ValueError):
        store.save(
            conn,
            {
                "default_proxy": "googlebot",
                "min_chars": 3000,
                "rules": [{"host": "https://wsj.com/article"}],
            },
        )


def test_save_rejects_custom_template_with_extra_placeholder(env: Path) -> None:
    # A second placeholder passes a naive {url}-substring check but crashes .format().
    database.run_migrations(env)
    with database.connection(env) as conn, pytest.raises(ValueError):
        store.save(
            conn,
            {
                "default_proxy": "googlebot",
                "min_chars": 3000,
                "rules": [
                    {
                        "host": "x.com",
                        "proxy": "custom",
                        "custom_template": "https://r.example/{url}?k={key}",
                    }
                ],
            },
        )


def test_save_accepts_valid_custom_template(env: Path) -> None:
    database.run_migrations(env)
    with database.connection(env) as conn:
        cfg = store.save(
            conn,
            {
                "default_proxy": "googlebot",
                "min_chars": 3000,
                "rules": [
                    {
                        "host": "x.com",
                        "proxy": "custom",
                        "custom_template": "https://r.example/{url}",
                    }
                ],
            },
        )
    assert cfg["rules"][0]["custom_template"] == "https://r.example/{url}"
