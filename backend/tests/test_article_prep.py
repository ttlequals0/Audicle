from __future__ import annotations

from app.services import article_prep

_WIKI = """\
# CoreWeave

CoreWeave is a cloud computing company.[1] It was founded in 2017.[12]

## Contents

- [History](#History)
- [Operations](#Operations)
- [See also](#See_also)

## History [edit](https://en.wikipedia.org/edit)

CoreWeave grew rapidly through 2024 and 2025.

## Operations[edit]

It runs GPU data centers.[\\[2\\]](#cite_note-2)

## See also

- [Nvidia](https://en.wikipedia.org/wiki/Nvidia)
- [Cloud computing](https://en.wikipedia.org/wiki/Cloud_computing)

## References

1. Some citation.
2. Another citation.

## External links

- [Official site](https://coreweave.com)
"""


def test_strip_chrome_removes_toc_appendix_and_markers() -> None:
    out = article_prep.strip_chrome(_WIKI)
    # Body survives.
    assert "CoreWeave is a cloud computing company." in out
    assert "CoreWeave grew rapidly through 2024 and 2025." in out
    assert "It runs GPU data centers." in out
    # Section headings the LLM will turn into transitions are kept...
    assert "History" in out
    assert "Operations" in out
    # ...but their chrome is gone.
    assert "[edit]" not in out
    assert "edit](" not in out
    # Citation superscripts removed.
    assert "[1]" not in out
    assert "[12]" not in out
    assert "cite_note" not in out
    # TOC and all trailing appendix sections removed wholesale.
    assert "#History" not in out
    assert "See also" not in out
    assert "## References" not in out
    assert "External links" not in out
    assert "coreweave.com" not in out
    assert "Official site" not in out


def test_strip_chrome_preserves_plain_prose() -> None:
    prose = (
        "The first paragraph has no chrome at all.\n\n"
        "A second paragraph mentions references in passing but is not a heading."
    )
    assert article_prep.strip_chrome(prose) == prose


def test_strip_chrome_keeps_real_link_with_numeric_label() -> None:
    # A genuine markdown link whose label is a number must not be mistaken for a
    # citation superscript.
    text = "See [2024](https://example.com/2024) results."
    assert article_prep.strip_chrome(text) == text


def test_strip_chrome_empty_input() -> None:
    assert article_prep.strip_chrome("") == ""


def test_strip_inline_markdown_keeps_link_label_drops_url() -> None:
    text = "Read the [full report](https://example.com/r.pdf) now."
    assert article_prep.strip_inline_markdown(text) == "Read the full report now."


def test_strip_inline_markdown_removes_images() -> None:
    text = "Before ![a chart](https://example.com/c.png) after."
    assert article_prep.strip_inline_markdown(text) == "Before  after."


def test_strip_inline_markdown_leaves_plain_prose_untouched() -> None:
    text = "A sentence with a * star and an under_score, no links."
    assert article_prep.strip_inline_markdown(text) == text


def test_strip_boilerplate_removes_ad_markers() -> None:
    assert "REG AD" not in article_prep.strip_boilerplate("REG AD\n\nThe real article body here.")


def test_strip_boilerplate_removes_skip_and_jump_nav() -> None:
    out = article_prep.strip_boilerplate("Jump to main content\n\nThe body of the story.")
    assert "Jump to main content" not in out and "The body of the story." in out


def test_strip_boilerplate_removes_dateline_but_keeps_body() -> None:
    out = article_prep.strip_boilerplate("Published Thu 2 Jul 2026 // 08:00 UTC\n\nThe article body.")
    assert "UTC" not in out and "The article body." in out


def test_strip_boilerplate_removes_tipline_with_email() -> None:
    text = (
        "Have a story? Share it with us at pwned@sitpub.com. Anonymity is available upon request."
        "\n\nThe real article body about the incident."
    )
    out = article_prep.strip_boilerplate(text)
    assert "pwned@sitpub.com" not in out
    assert "Share it with us" not in out
    assert "The real article body about the incident." in out


def test_strip_boilerplate_removes_subscribe_line() -> None:
    assert "newsletter" not in article_prep.strip_boilerplate("Sign up for our newsletter\n\nBody text.")


def test_strip_boilerplate_keeps_long_paragraph_with_incidental_cue() -> None:
    # A long real paragraph containing a dateline-ish cue ("published ... EST") is NOT
    # deleted -- only short, chrome-sized lines are stripped.
    para = (
        "The committee published its findings at 9am EST after a long inquiry, and the "
        "report went on to describe in considerable detail how the winter budget would be "
        "allocated across the various departments over the coming fiscal year and beyond."
    )
    assert "winter budget would be allocated" in article_prep.strip_boilerplate(para)


def test_strip_boilerplate_leaves_plain_prose_untouched() -> None:
    # "published", "share", "by" appear but not as cruft cues -> nothing is cut.
    prose = (
        "The mayor published the budget on Tuesday. Residents can share their views at the hearing."
        "\n\nA second paragraph written by the council with no cruft at all."
    )
    assert article_prep.strip_boilerplate(prose) == prose


def test_prose_chars_scores_archive_toolbar_near_zero(archive_capture_junk: str) -> None:
    # Clears the 500-char floor on length, but the only sentence is the bot-wall line.
    assert len(archive_capture_junk) > 500
    assert article_prep.prose_chars(archive_capture_junk) == len(
        "Please enable JS and disable any ad blocker"
    )


def test_prose_chars_counts_article_paragraphs_in_full() -> None:
    paragraph = (
        "The build-out of data centers has become one of the largest capital projects "
        "in the country, and economists are split on what happens when it slows."
    )
    markdown = f"# Headline\n\n{paragraph}\n\n[Share](https://x.test/s)\n\n{paragraph}"
    assert article_prep.prose_chars(markdown) == 2 * len(paragraph)


def test_prose_chars_counts_link_labels_not_urls() -> None:
    line = "Read the [full report from the central bank](https://bank.test/r) before the vote next week."
    assert article_prep.prose_chars(line) == len(
        "Read the full report from the central bank before the vote next week."
    )


def test_prose_chars_counts_unspaced_scripts() -> None:
    # CJK prose has no spaces, so the whole sentence is one token.
    sentence = "人工知能への投資は米国史上最大の経済的な賭けになりつつあると専門家は指摘している。" * 2
    assert article_prep.prose_chars(sentence) == len(sentence)


def test_prose_chars_stops_at_limit() -> None:
    paragraph = "One sentence of the article that is long enough to count as prose here. "
    markdown = "\n\n".join([paragraph] * 50)
    assert article_prep.prose_chars(markdown, limit=100) < 2 * len(paragraph)


def test_prose_chars_counts_list_items_with_numbers() -> None:
    item = "Mix 2 cups of flour with the sugar and 3 eggs until smooth."
    assert article_prep.prose_chars(f"- {item}\n1. {item}") == 2 * len(item)
