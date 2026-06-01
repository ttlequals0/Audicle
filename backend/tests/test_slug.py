from __future__ import annotations

from app.services.slug import slugify


def test_slugify_basic_title() -> None:
    assert slugify("Articles of Interest") == "articles_of_interest"


def test_slugify_lowercases_and_collapses_runs() -> None:
    assert slugify("  My   Great_Feed!! ") == "my_great_feed"
    assert slugify("A -- B / C") == "a_b_c"


def test_slugify_strips_leading_trailing_separators() -> None:
    assert slugify("!!!Hello, World!!!") == "hello_world"
    assert slugify("___edge___") == "edge"


def test_slugify_folds_unicode_to_ascii() -> None:
    assert slugify("Café Crème") == "cafe_creme"


def test_slugify_default_when_empty_or_unrepresentable() -> None:
    assert slugify("") == "audicle"
    assert slugify("   ") == "audicle"
    assert slugify("日本語") == "audicle"
    assert slugify("!!!") == "audicle"


def test_slugify_caps_length_and_trims_trailing_separator() -> None:
    out = slugify("word " * 40)  # far longer than 80 chars
    assert len(out) <= 80
    assert not out.endswith("_")
    assert out.startswith("word_word")
