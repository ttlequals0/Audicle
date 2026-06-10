from __future__ import annotations

from app.services import cleanup_output

# --- is_empty_section ------------------------------------------------------


def test_is_empty_section_detects_sentinel_and_disclaimers() -> None:
    assert cleanup_output.is_empty_section("NO_ARTICLE_CONTENT")
    assert cleanup_output.is_empty_section("NO_ARTICLE_CONTENT.")
    assert cleanup_output.is_empty_section('"NO_ARTICLE_CONTENT"')
    assert cleanup_output.is_empty_section('"NO_ARTICLE_CONTENT".')
    # The two real disclaimers the model leaked in the incident.
    assert cleanup_output.is_empty_section(
        "There is no article content in what you provided. The entire text is "
        "website cookie-consent and privacy-policy boilerplate."
    )
    assert cleanup_output.is_empty_section(
        "If you paste the article body text, I can clean it for you."
    )


def test_is_empty_section_keeps_real_prose() -> None:
    # Normal narration is not dropped, even when it mentions "article".
    assert not cleanup_output.is_empty_section(
        "This article explains how the kernel boots in six phases. " * 5
    )
    assert not cleanup_output.is_empty_section(
        "The mayor announced a five hundred thousand dollar settlement today."
    )
    # A real column that opens "There is no article this week" must survive: it
    # hits the disclaimer opener but has no supplied-input signal.
    assert not cleanup_output.is_empty_section(
        "There is no article this week, so instead we round up the month's best reads."
    )


def test_is_empty_section_catches_sentinel_with_stray_prose() -> None:
    # Model adds stray text around the sentinel -> still dropped (containment).
    assert cleanup_output.is_empty_section("NO_ARTICLE_CONTENT\n\nSkip to main content")


# --- extract_clean_output --------------------------------------------------


def test_extract_clean_output_slices_between_markers() -> None:
    raw = (
        "I don't have any stored instructions for how to clean articles. Could "
        "you point me to the rules?\n\n"
        "<<<AUDICLE_BEGIN>>>\nThe mayor announced the budget today.\n<<<AUDICLE_END>>>\n"
        "Let me know if you want more."
    )
    # Preamble before the markers and trailing slop after are both dropped.
    assert (
        cleanup_output.extract_clean_output(raw)
        == "The mayor announced the budget today."
    )


def test_extract_clean_output_stops_at_first_end_marker() -> None:
    # Model emits the end marker, then trailing slop that repeats the end token.
    # The slice must stop at the FIRST end marker, not the last (rfind would keep
    # the intervening slop).
    raw = (
        "<<<AUDICLE_BEGIN>>>\nThe mayor announced the budget today.\n<<<AUDICLE_END>>>\n"
        "Want me to do another? <<<AUDICLE_END>>>"
    )
    assert (
        cleanup_output.extract_clean_output(raw) == "The mayor announced the budget today."
    )


def test_extract_clean_output_open_marker_only() -> None:
    # A truncated response missing the closing marker keeps everything after the
    # opening marker rather than losing the window.
    raw = "<<<AUDICLE_BEGIN>>>\nThe council met on Tuesday to vote."
    assert (
        cleanup_output.extract_clean_output(raw) == "The council met on Tuesday to vote."
    )


def test_extract_clean_output_no_markers_strips_leading_preamble() -> None:
    raw = (
        "Sure, here is the cleaned article.\n\n"
        "The committee released its findings this morning.\n\n"
        "A second paragraph continues the story."
    )
    out = cleanup_output.extract_clean_output(raw)
    assert out.startswith("The committee released its findings")
    assert "Sure, here is" not in out


def test_extract_clean_output_scrubs_stray_marker_tokens() -> None:
    # A stray marker token with no opening marker is removed (the surrounding
    # spaces stay -- TTS normalization collapses whitespace later).
    raw = "The report <<<AUDICLE_END>>> landed today."
    assert "AUDICLE" not in cleanup_output.extract_clean_output(raw)
    assert cleanup_output.extract_clean_output(raw).startswith("The report")


# --- is_refusal_output -----------------------------------------------------


def test_is_refusal_output_detects_short_assistant_speak() -> None:
    assert cleanup_output.is_refusal_output(
        "I don't have any stored instructions for how to clean articles."
    )
    assert cleanup_output.is_refusal_output("Could you point me to the specific rules?")
    # A long real article that merely opens with "Sure," is not a refusal.
    assert not cleanup_output.is_refusal_output("Sure enough, the bridge held. " * 40)


# --- needs_compliance_retry ------------------------------------------------


def test_needs_compliance_retry_trusts_markers_and_sentinel() -> None:
    # Markers honored -> no retry, even if a preamble was glued on top.
    assert not cleanup_output.needs_compliance_retry(
        "preamble\n<<<AUDICLE_BEGIN>>>\nbody\n<<<AUDICLE_END>>>", "body"
    )
    # Explicit sentinel is a valid "boilerplate only" answer -> no retry.
    assert not cleanup_output.needs_compliance_retry("NO_ARTICLE_CONTENT", "")
    # Bare conversational refusal (no markers, no sentinel) -> retry.
    assert cleanup_output.needs_compliance_retry(
        "I don't have any stored instructions.", "I don't have any stored instructions."
    )
