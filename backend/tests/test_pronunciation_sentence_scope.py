"""Opt-in sentence-scoped pronunciation pass (PRONUNCIATION_SCOPE=sentence).

Default scope stays "chunk" (full-window protocol, unchanged). When set to
"sentence", only reference-matched sentences are sent to the LLM as numbered
lines, and the reply is spliced back positionally. Any protocol or splice
failure falls back to the existing full-window call.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import get_settings
from app.core import database
from app.services import cleanup_output, lexicon, pipeline

BEGIN = cleanup_output.BEGIN_MARKER
END = cleanup_output.END_MARKER

_WINDOW = "I ran a SQL query today. A plain sentence follows here."


def _sentence_scope_settings():
    return get_settings().model_copy(update={"PRONUNCIATION_SCOPE": "sentence"})


async def test_sentence_scope_sends_only_matched_sentences(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)
    captured: list[str] = []

    async def _fake(_system, user, _settings, **_kwargs):
        captured.append(user)
        return f"{BEGIN}\n1. I ran a sequel query today.\n{END}"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    await pipeline._pronounce_chunks_with_llm(
        "job", [_WINDOW], _sentence_scope_settings()
    )
    assert len(captured) == 1
    assert "A plain sentence follows here." not in captured[0]
    assert "I ran a SQL query today." in captured[0]


async def test_sentence_scope_splices_reply_and_keeps_unmatched_byte_identical(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)

    async def _fake(_system, _user, _settings, **_kwargs):
        return f"{BEGIN}\n1. I ran a sequel query today.\n{END}"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    out = await pipeline._pronounce_chunks_with_llm(
        "job", [_WINDOW], _sentence_scope_settings()
    )
    assert out == ["I ran a sequel query today. A plain sentence follows here."]


async def test_sentence_scope_wrong_numbering_falls_back_to_full_window(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.run_migrations(env)
    calls = {"n": 0}

    async def _fake(_system, user, _settings, **_kwargs):
        calls["n"] += 1
        if "<text>\n" in user:
            # Full-window fallback protocol: echo the window verbatim.
            body = user.split("<text>\n", 1)[1].rsplit("\n</text>", 1)[0]
            return f"{BEGIN}\n{body}\n{END}"
        # Sentence-scope protocol, but with the wrong number (should be "1.").
        return f"{BEGIN}\n2. I ran a sequel query today.\n{END}"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    out = await pipeline._pronounce_chunks_with_llm(
        "job", [_WINDOW], _sentence_scope_settings()
    )
    assert calls["n"] == 2
    assert out == [_WINDOW]  # fell back to the full-window (unchanged) result


async def test_sentence_scope_splice_failure_falls_back_to_full_window(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the selected 'original' sentence text isn't actually locatable in
    the window (str.find fails), that's a splice failure and falls back."""

    database.run_migrations(env)
    calls = {"n": 0}

    async def _fake(_system, user, _settings, **_kwargs):
        calls["n"] += 1
        if "<text>\n" in user:
            body = user.split("<text>\n", 1)[1].rsplit("\n</text>", 1)[0]
            return f"{BEGIN}\n{body}\n{END}"
        return f"{BEGIN}\n1. I ran a sequel query today.\n{END}"

    # Force a selected "sentence" that is not a literal substring of the
    # window, simulating the one scenario str.find can't locate.
    monkeypatch.setattr(pipeline.chunker, "split_sentences", lambda _p: ["I ran an SQL query today."])
    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    out = await pipeline._pronounce_chunks_with_llm(
        "job", [_WINDOW], _sentence_scope_settings()
    )
    assert calls["n"] == 2
    assert out == [_WINDOW]


async def test_sentence_scope_strips_whitespace_after_number(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extra whitespace between the "N." prefix and the respelled text (e.g.
    "1.   text") must not be spliced into the narration verbatim."""

    database.run_migrations(env)

    async def _fake(_system, _user, _settings, **_kwargs):
        return f"{BEGIN}\n1.   I ran a sequel query today.\n{END}"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    out = await pipeline._pronounce_chunks_with_llm(
        "job", [_WINDOW], _sentence_scope_settings()
    )
    assert out == ["I ran a sequel query today. A plain sentence follows here."]


async def test_sentence_scope_min_ratio_reverts_one_sentence_without_fallback(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A respelled sentence far shorter than the original (below
    _PRONUNCIATION_MIN_RATIO) reverts to the original sentence text, but does
    not fail the whole splice or trigger the full-window fallback."""

    database.run_migrations(env)
    calls = {"n": 0}

    async def _fake(_system, _user, _settings, **_kwargs):
        calls["n"] += 1
        # "SQL." is far shorter than half of "I ran a SQL query today." --
        # the per-sentence min-ratio guard should keep the original instead.
        return f"{BEGIN}\n1. SQL.\n{END}"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    out = await pipeline._pronounce_chunks_with_llm(
        "job", [_WINDOW], _sentence_scope_settings()
    )
    assert calls["n"] == 1  # no fallback call -- the splice itself succeeded
    assert out == [_WINDOW]  # matched sentence reverted, window unchanged


async def test_chunk_scope_behavior_unchanged_by_default(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default PRONUNCIATION_SCOPE is 'chunk': the sentence-numbering protocol
    is never sent, only the existing full-window <text> protocol."""

    database.run_migrations(env)
    captured: list[str] = []

    async def _fake(_system, user, _settings, **_kwargs):
        captured.append(user)
        body = user.split("<text>\n", 1)[1].rsplit("\n</text>", 1)[0]
        return f"{BEGIN}\n{body}\n{END}"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    settings = get_settings()
    assert settings.PRONUNCIATION_SCOPE == "chunk"
    out = await pipeline._pronounce_chunks_with_llm("job", [_WINDOW], settings)
    assert len(captured) == 1
    assert "<text>\n" in captured[0]
    assert out == [_WINDOW]


async def test_sentence_scope_uses_its_own_system_prompt_with_the_reference(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full-window system prompt orders the model to output the ENTIRE
    text, which contradicts returning a subset as numbered lines. The
    sentence-scope call gets its own prompt, still carrying the filtered
    reference lines."""

    database.run_migrations(env)
    systems: list[str] = []

    async def _fake(system, _user, _settings, **_kwargs):
        systems.append(system)
        return f"{BEGIN}\n1. I ran a sequel query today.\n{END}"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    await pipeline._pronounce_chunks_with_llm("job", [_WINDOW], _sentence_scope_settings())

    assert len(systems) == 1
    assert "ENTIRE text" not in systems[0]
    assert "numbered lines" in systems[0]
    assert "PRONUNCIATION REFERENCE (term -> respelling):" in systems[0]
    assert "SQL" in systems[0]


async def test_sentence_scope_falls_back_when_a_term_spans_a_sentence_split(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A term with an internal abbreviation period ("St. John") is split across
    two sentences, so no single sentence matches it. The window still contains
    the term, so it goes to the chunk-scoped call instead of being returned
    unrespelled."""

    database.run_migrations(env)
    with database.connection(get_settings().DATA_DIR) as conn:
        lexicon.replace_user_entries(conn, {"St. John": {"spoken": "Saint John"}})

    window = "We met at St. John Street today. A plain sentence follows here."
    captured: list[str] = []

    async def _fake(_system, user, _settings, **_kwargs):
        captured.append(user)
        body = user.split("<text>\n", 1)[1].rsplit("\n</text>", 1)[0]
        return f"{BEGIN}\n{body.replace('St. John', 'Saint John')}\n{END}"

    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    out = await pipeline._pronounce_chunks_with_llm("job", [window], _sentence_scope_settings())

    assert len(captured) == 1
    assert "<text>\n" in captured[0]  # the chunk-scoped protocol
    assert out == ["We met at Saint John Street today. A plain sentence follows here."]


async def test_sentence_scope_protocol_failure_latches_off_for_the_job(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that ignores the numbered-line contract will ignore it again,
    so after the first protocol failure the job stops paying for a doomed
    sentence call plus a full-window retry on every remaining chunk."""

    database.run_migrations(env)
    settings = get_settings().model_copy(
        update={"PRONUNCIATION_SCOPE": "sentence", "LLM_PRONUNCIATION_CONCURRENCY": 1}
    )
    captured: list[str] = []

    async def _fake(_system, user, _settings, **_kwargs):
        captured.append(user)
        if "<text>\n" in user:
            body = user.split("<text>\n", 1)[1].rsplit("\n</text>", 1)[0]
            return f"{BEGIN}\n{body}\n{END}"
        return "1. I ran a sequel query today."  # markers ignored: protocol failure

    monkeypatch.setattr(pipeline, "_llm_with_retry", _fake)
    second = "The SQL migration finished last night. Another plain sentence."
    out = await pipeline._pronounce_chunks_with_llm("job", [_WINDOW, second], settings)

    kinds = ["chunk" if "<text>\n" in user else "sentence" for user in captured]
    # Window 1: sentence attempt, then its full-window fallback. Window 2:
    # straight to chunk scope, no second sentence attempt.
    assert kinds == ["sentence", "chunk", "chunk"]
    assert out == [_WINDOW, second]
