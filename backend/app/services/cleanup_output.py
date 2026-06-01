"""Parsing and classification of one cleanup window's raw LLM response.

The cleanup stage runs the article in windows, one LLM call each. This module
owns the contract for what comes back: the begin/end markers the narration is
wrapped in, the fallback that strips a conversational preamble when the model
ignores them, and the predicates that decide whether a window is junk (the
NO_ARTICLE_CONTENT sentinel, a "there is no article" disclaimer, or a short
assistant-speak refusal) and whether to retry it once. Kept out of pipeline.py
so the orchestrator stays focused on stage sequencing.
"""

from __future__ import annotations

import re

# The cleanup prompt tells the model to emit this exact token for a window that
# is all boilerplate (no article body). An explicit sentinel is obeyed far more
# reliably than "return nothing", which models tend to answer with a prose
# disclaimer ("There is no article content...") that would otherwise be narrated.
EMPTY_SECTION_SENTINEL = "NO_ARTICLE_CONTENT"

# The cleanup prompt tells the model to wrap the cleaned narration between these
# markers. The parser keeps only the text between them, so any conversational
# preamble the model glues on top (e.g. "I don't have any stored instructions...")
# is discarded instead of being narrated. A rare sentinel pair is used rather than
# JSON because the payload is the full article body -- JSON would force escaping
# every newline/quote and a truncated response would void the whole window.
BEGIN_MARKER = "<<<AUDICLE_BEGIN>>>"
END_MARKER = "<<<AUDICLE_END>>>"

# Prepended to the window on the one compliance retry when the model ignored the
# markers and answered conversationally.
RETRY_INSTRUCTION = (
    "Your previous response was rejected because it was not the cleaned article. "
    f"Output ONLY the cleaned narration between {BEGIN_MARKER} and {END_MARKER}, "
    "with no other text, question, or explanation. If the passage is only "
    "boilerplate, output exactly NO_ARTICLE_CONTENT.\n\n"
)

_MARKER_SCRUB_RE = re.compile(re.escape(BEGIN_MARKER) + "|" + re.escape(END_MARKER))

# Backstops for when the model ignores the sentinel and writes a refusal in prose.
# Assistant-offer phrasing ("if you paste the article...") is conclusive on its
# own -- a real article never says it. The "there is no article..." opener can
# appear in a real column, so it additionally requires a reference to the supplied
# input (or a boilerplate keyword) before a window is dropped.
_DISCLAIMER_OFFER_RE = re.compile(
    r"^if you (paste|provide|share) the article", re.IGNORECASE
)
_DISCLAIMER_OPENER_RE = re.compile(
    r"^(there (is|was) no (article|content|text)|"
    r"no article (body|content|text) (was|is|were))",
    re.IGNORECASE,
)
_DISCLAIMER_SIGNAL_RE = re.compile(
    r"you (provided|shared|gave|pasted)|in (the content|what you)|"
    r"was not included|to extract|cookie|consent|boilerplate|navigation",
    re.IGNORECASE,
)
# No-marker fallback: drop a leading conversational paragraph the model emitted
# instead of honoring the markers. Patterns are assistant-speak a real article
# never opens with, so prose is not at risk.
_PREAMBLE_RE = re.compile(
    r"^(?:"
    r"i (?:don't|do not|cannot|can't|won't|am unable|'m unable)\b"
    r"|i'?m (?:sorry|an ai|unable)\b"
    r"|could you (?:please )?(?:point|provide|share|clarify|specify)\b"
    r"|sure[,!.: ]"
    r"|certainly[,!.: ]"
    r"|here (?:is|are) (?:the|your)\b"
    r"|as an ai\b"
    r")",
    re.IGNORECASE,
)


def _strip_preamble(text: str) -> str:
    """Drop leading conversational paragraphs (no-marker fallback)."""

    paragraphs = re.split(r"\n\s*\n", text)
    while len(paragraphs) > 1 and _PREAMBLE_RE.match(paragraphs[0].strip()):
        paragraphs.pop(0)
    return "\n\n".join(paragraphs).strip()


def extract_clean_output(raw: str) -> str:
    """Pull the cleaned narration out of one window's raw LLM response.

    Slices between the begin/end markers (dropping any preamble or trailing
    slop); if the model ignored the markers, strips a leading conversational
    paragraph as a fallback. Any stray marker token is scrubbed either way.
    """

    text = raw.strip()
    begin = text.find(BEGIN_MARKER)
    if begin != -1:
        body = text[begin + len(BEGIN_MARKER) :]
        end = body.rfind(END_MARKER)
        if end != -1:
            body = body[:end]
        result = body
    else:
        result = _strip_preamble(text)
    return _MARKER_SCRUB_RE.sub("", result).strip()


def is_empty_section(text: str) -> bool:
    """True when a cleanup window produced no article body -- the sentinel
    (anywhere in the output, in case the model adds stray prose), or a short
    refusal the model wrote instead of the sentinel."""

    if EMPTY_SECTION_SENTINEL in text:
        return True
    stripped = text.strip()
    # Backstop only: a short output (a real article section is long). The offer
    # phrasing is conclusive; the "no article" opener needs a supplied-input
    # signal so a real "There is no article this week..." column is not dropped.
    if len(stripped) >= 600:
        return False
    if _DISCLAIMER_OFFER_RE.match(stripped):
        return True
    return bool(_DISCLAIMER_OPENER_RE.match(stripped)) and bool(
        _DISCLAIMER_SIGNAL_RE.search(stripped)
    )


def is_refusal_output(text: str) -> bool:
    """A short output that opens with assistant-speak ("I don't have...", "Sure,
    here is...") -- a conversational refusal the model emitted instead of
    narration, distinct from the NO_ARTICLE_CONTENT sentinel and the known
    "there is no article" disclaimers that ``is_empty_section`` already covers."""

    stripped = text.strip()
    return len(stripped) < 600 and bool(_PREAMBLE_RE.match(stripped))


def needs_compliance_retry(raw: str, part: str) -> bool:
    """Retry a window once when the model ignored the marker contract and gave a
    refusal rather than the explicit NO_ARTICLE_CONTENT sentinel (a valid answer)
    or a recognized disclaimer (already dropped). A marker'd window is trusted."""

    if BEGIN_MARKER in raw or EMPTY_SECTION_SENTINEL in raw:
        return False
    return not part or is_refusal_output(part)
