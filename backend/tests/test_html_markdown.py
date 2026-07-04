from __future__ import annotations

import pytest
from app.services import html_markdown


def _page(article_paras: int, sidebar_teasers: int = 40) -> str:
    sidebar = "<aside><h3>MOST POPULAR</h3>" + "".join(
        f"<p>Teaser headline number {i}</p>" for i in range(sidebar_teasers)
    ) + "</aside>"
    article = "<article>" + "".join(
        f"<p>Sentence {i} of the real article body about shoveling snow.</p>"
        for i in range(article_paras)
    ) + "</article>"
    return f"<html><body>{sidebar}{article}</body></html>"


# --- helpers ---------------------------------------------------------------


def test_largest_article_extracts_block_text() -> None:
    html = "<article><p>tiny</p></article>" + _page(30)
    article = html_markdown._largest_article(html)
    assert article is not None
    md = html_markdown._article_block_markdown(article)
    assert "real article body" in md
    assert md.count("\n\n") > 5  # paragraph structure preserved


def test_largest_article_none_without_article() -> None:
    assert html_markdown._largest_article("<div><p>no article element here</p></div>") is None


def test_trafilatura_missed_article_true_when_disjoint() -> None:
    assert html_markdown._trafilatura_missed_article(
        "MOST POPULAR a list of other stories and teasers " * 5,
        "The actual article body about shoveling snow and network access " * 20,
    )


def test_trafilatura_missed_article_false_when_contained() -> None:
    article = "The actual article body about shoveling snow and network access. " * 20
    assert not html_markdown._trafilatura_missed_article(article[:300], article)


def test_trafilatura_not_missed_when_only_title_prefix_is_outside_article() -> None:
    # trafilatura leads with a title/byline that lives in a <header> outside <article>;
    # the middle sample still lands in the body, so this is NOT a miss.
    body = "The council debated the winter budget in great detail all evening. " * 10
    trafi = "Winter Budget Debate Rages On. " + body
    assert not html_markdown._trafilatura_missed_article(trafi, body)


def test_trafilatura_not_missed_on_minified_article_text() -> None:
    # A minified <article> has no inter-tag spaces; trafilatura adds them. Dropping all
    # non-alphanumerics makes them compare equal.
    article_text = "Thecouncildebatedthewinterbudgetingreatdetail" * 5
    trafi = "The council debated the winter budget in great detail " * 5
    assert not html_markdown._trafilatura_missed_article(trafi, article_text)


# --- html_to_markdown override decision (trafilatura mocked for determinism) ---


def _mock_trafilatura(monkeypatch: pytest.MonkeyPatch, extract_result: str) -> None:
    monkeypatch.setattr(html_markdown.trafilatura, "extract", lambda *a, **k: extract_result)
    monkeypatch.setattr(html_markdown.trafilatura, "extract_metadata", lambda *a, **k: None)


def test_override_when_trafilatura_picked_wrong_node(monkeypatch: pytest.MonkeyPatch) -> None:
    # trafilatura returned the sidebar rail (not in <article>); the fallback recovers it.
    _mock_trafilatura(monkeypatch, "MOST POPULAR teaser list that is not in the article " * 8)
    md, _meta = html_markdown.html_to_markdown(_page(30))
    assert "real article body" in md


def test_keeps_trafilatura_when_it_got_the_article(monkeypatch: pytest.MonkeyPatch) -> None:
    # trafilatura's output IS inside <article> (the same sentences, in order) -> no
    # override, its text stands verbatim.
    good = " ".join(
        f"Sentence {i} of the real article body about shoveling snow." for i in range(12)
    )
    _mock_trafilatura(monkeypatch, good)
    md, _meta = html_markdown.html_to_markdown(_page(30))
    assert md == good


def test_keeps_long_trafilatura_over_small_decoy_article(monkeypatch: pytest.MonkeyPatch) -> None:
    # Body lives in <main> (no <article> wrapper); a small decoy <article> exists. The
    # size guard must keep trafilatura's long extraction, not downgrade to the decoy.
    body = "<main>" + "".join(
        f"<p>The winter budget paragraph number {i} with plenty of real detail.</p>"
        for i in range(30)
    ) + "</main>"
    decoy = "<article>" + "".join(
        f"<p>Unrelated reader comment number {i} posted here today.</p>" for i in range(15)
    ) + "</article>"
    long_body = "The winter budget paragraph with plenty of real detail. " * 40
    _mock_trafilatura(monkeypatch, long_body)
    md, _meta = html_markdown.html_to_markdown(f"<html><body>{decoy}{body}</body></html>")
    assert "winter budget" in md  # trafilatura's long extraction kept
    assert "reader comment" not in md  # small decoy <article> did not win
