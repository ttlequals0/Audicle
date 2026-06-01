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
