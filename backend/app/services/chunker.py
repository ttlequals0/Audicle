"""Hybrid text chunker.

Order of fallback when a paragraph doesn't fit:

1. Split on paragraph boundaries (``\\n\\n``). Greedy-pack into chunks of
   ``TTS_CHUNK_TARGET_WORDS`` words. Glued run-ons (``end.Next``) are healed
   into ``end. Next`` first so the sentence splitter can see them.
2. If a single sentence (or paragraph treated as one) exceeds the target,
   split it on sentence boundaries.
3. If a sentence still exceeds the max, fall back to comma / semicolon
   splits and emit a WARN log so an article with many such cases is
   visible in Loki (event=chunk_fallback_split).
4. If a piece still has no comma / semicolon to break on, fall back to a
   whitespace split (event=chunk_whitespace_split). This preserves every
   word -- it never truncates -- and only risks a less natural pause.
5. Only a single whitespace-free token longer than ``TTS_CHUNK_MAX_CHARS``
   is genuinely unsplittable; that raises :class:`UnsplittableSentenceError`
   so the pipeline marks the job ``failed`` with stage=chunk and a
   sentence-preview error rather than forcing a mid-word cut.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.config import Settings

logger = logging.getLogger("app.services.chunker")

# Crude sentence boundary: end-of-sentence punctuation followed by whitespace.
# Misses Latin abbreviations (Dr., e.g., etc.) but works well enough on the
# cleanup-stage output where the LLM has already normalized inline
# abbreviations into spoken form.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_COMMA_OR_SEMI = re.compile(r"\s*([;,])\s+")

# Run-on boundary: extraction sometimes glues two sentences with no space
# ("...end.Next..."). The whitespace-required _SENTENCE_BOUNDARY can't split
# those, so the run-on stays one oversized "sentence" and is pushed to the
# comma/semicolon fallback or fails. Insert a space only at a real boundary:
# a lowercase letter or digit, then .!?, an optional closing quote/bracket,
# then an uppercase letter. The lowercase/digit prefix spares all-caps
# abbreviations ("U.S.A", uppercase before the dot) and the uppercase suffix
# spares decimals ("3.14", a digit follows). English-targeted: Python's re has
# no \p{Lu}, so this uses ASCII case classes, which is fine for the
# English-only narration the cleanup stage produces.
_RUNON_BOUNDARY = re.compile(r"([a-z0-9])([.!?])([\"')\]]?)([A-Z])")


class UnsplittableSentenceError(Exception):
    """Raised when no available breakpoint can fit a sentence under the limit.

    The pipeline converts this to a stage=chunk failure with a clear
    operator-facing message including the offending sentence preview.
    """

    def __init__(self, sentence: str, word_count: int, char_count: int) -> None:
        preview = sentence[:120] + ("..." if len(sentence) > 120 else "")
        super().__init__(
            f"sentence is unsplittable ({word_count} words, {char_count} chars) "
            f"with no comma or semicolon breakpoint: {preview!r}"
        )
        self.sentence_preview = preview
        self.word_count = word_count
        self.char_count = char_count


@dataclass(frozen=True)
class ChunkerLimits:
    target_words: int
    max_words: int
    max_chars: int

    @classmethod
    def from_settings(cls, settings: Settings) -> ChunkerLimits:
        return cls(
            target_words=settings.TTS_CHUNK_TARGET_WORDS,
            max_words=settings.TTS_CHUNK_MAX_WORDS,
            max_chars=settings.TTS_CHUNK_MAX_CHARS,
        )


def chunk(text: str, settings: Settings) -> list[str]:
    """Split ``text`` into TTS-sized chunks per the build-plan rules.

    Returns a list of chunk strings, each greedy-packed to about
    ``TTS_CHUNK_TARGET_WORDS`` words. Empty input returns an empty list.
    """

    limits = ChunkerLimits.from_settings(settings)
    chunks: list[str] = []
    for paragraph in _split_paragraphs(text):
        # Thread the running output-chunk position into _pack so the
        # chunk_index field in chunk_fallback_split WARN logs reflects the
        # global position in the article, not a per-paragraph 0-reset.
        chunks.extend(_chunk_paragraph(paragraph, limits, base_chunk_index=len(chunks)))
    return chunks


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def pack_paragraphs(text: str, max_chars: int) -> list[str]:
    """Greedy-pack paragraphs into windows no larger than ``max_chars``.

    Used by the cleanup stage to process long articles in bounded windows so a
    single LLM call's output never hits the token cap. Reuses the paragraph
    splitter. A lone paragraph that exceeds ``max_chars`` becomes its own window
    (we don't sub-split prose mid-paragraph here; the LLM call still handles it).
    Empty input returns an empty list.
    """

    windows: list[str] = []
    current: list[str] = []
    current_chars = 0
    for paragraph in _split_paragraphs(text):
        sep = 2 if current else 0  # the "\n\n" rejoin between paragraphs
        if current and current_chars + sep + len(paragraph) > max_chars:
            windows.append("\n\n".join(current))
            current = []
            current_chars = 0
            sep = 0
        current.append(paragraph)
        current_chars += sep + len(paragraph)
    if current:
        windows.append("\n\n".join(current))
    return windows


def _chunk_paragraph(paragraph: str, limits: ChunkerLimits, *, base_chunk_index: int) -> list[str]:
    # Heal glued run-ons ("end.Next" -> "end. Next") before measuring, so even a
    # short paragraph that fits in one chunk gets the boundary repaired instead of
    # sending the run-on to TTS unsplit (the early return below skips the sentence
    # splitter, which is the other place this would otherwise happen).
    paragraph = _insert_runon_boundaries(paragraph)
    word_count = len(paragraph.split())
    char_count = len(paragraph)
    if word_count <= limits.target_words and char_count <= limits.max_chars:
        return [paragraph]
    # Paragraph too big; switch to sentence-level packing.
    sentences = _split_sentences(paragraph)
    return _pack(sentences, limits, base_chunk_index=base_chunk_index)


def _insert_runon_boundaries(text: str) -> str:
    """Add a space at glued sentence boundaries (``end.Next`` -> ``end. Next``).

    See ``_RUNON_BOUNDARY`` for why this is conservative; it never touches
    decimals or all-caps abbreviations.
    """

    return _RUNON_BOUNDARY.sub(r"\1\2\3 \4", text)


def _split_sentences(paragraph: str) -> list[str]:
    # Run-on boundaries are already healed by _chunk_paragraph (the only caller)
    # before the text reaches here.
    sentences = _SENTENCE_BOUNDARY.split(paragraph)
    return [s.strip() for s in sentences if s.strip()]


def _pack(sentences: list[str], limits: ChunkerLimits, *, base_chunk_index: int) -> list[str]:
    """Greedy-pack sentences into chunks under the target size.

    A sentence that exceeds the max on its own triggers comma/semicolon
    fallback; an unsplittable sentence raises UnsplittableSentenceError.
    ``base_chunk_index`` is the running global chunk position; the local
    counter advances from there as new chunks are emitted, so the chunk_index
    field in ``chunk_fallback_split`` WARN records is the article-wide value.
    """

    chunks: list[str] = []
    current: list[str] = []
    current_words = 0
    current_chars = 0
    chunk_index = base_chunk_index

    def _flush() -> None:
        nonlocal current, current_words, current_chars, chunk_index
        if current:
            chunks.append(" ".join(current))
            current = []
            current_words = 0
            current_chars = 0
            chunk_index += 1

    for sentence in sentences:
        sent_words = len(sentence.split())
        sent_chars = len(sentence)

        if sent_words > limits.max_words or sent_chars > limits.max_chars:
            # This single sentence is over the limit. Flush whatever we
            # already had, then fall back to comma/semicolon splitting.
            _flush()
            for piece in _fallback_split_sentence(sentence, limits, chunk_index):
                chunks.append(piece)
                chunk_index += 1
            continue

        # If adding this sentence would exceed the target word count OR the
        # absolute char cap, flush first.
        if current and (
            current_words + sent_words > limits.target_words
            or current_chars + sent_chars + 1 > limits.max_chars
        ):
            _flush()
        current.append(sentence)
        current_words += sent_words
        current_chars += sent_chars + (1 if current_chars else 0)

    _flush()
    return chunks


def _fallback_split_sentence(sentence: str, limits: ChunkerLimits, chunk_index: int) -> list[str]:
    """Split a single sentence on commas / semicolons.

    Emits a structured WARN log so a steady-state stream of these gets
    surfaced in dashboards. Raises if even this fallback can't fit a piece
    under the max.
    """

    # ``re.split`` with a capturing group yields ['piece', sep, 'piece', sep, ...].
    # Pair each non-empty piece with the separator that follows it so we can
    # rejoin with the original punctuation (semicolons preserved, not
    # silently rewritten to commas).
    raw = _COMMA_OR_SEMI.split(sentence)
    pieces_with_sep: list[tuple[str, str]] = []
    i = 0
    while i < len(raw):
        text_part = raw[i].strip()
        sep = raw[i + 1] if i + 1 < len(raw) else ""
        if text_part:
            pieces_with_sep.append((text_part, sep))
        i += 2
    pieces = [p for p, _sep in pieces_with_sep]
    if len(pieces) <= 1:
        # No comma / semicolon to split on. Rather than fail the whole job,
        # fall back to a whitespace split: it preserves every word and only
        # risks a less natural pause. A single over-cap token still re-raises.
        return _fallback_split_on_whitespace(sentence, limits, chunk_index)

    logger.warning(
        "Chunk fallback: comma/semicolon split",
        extra={
            "event": "chunk_fallback_split",
            "sentence_len_words": len(sentence.split()),
            "sentence_len_chars": len(sentence),
            "chunk_index": chunk_index,
            "piece_count": len(pieces),
        },
    )

    # Re-pack the pieces under the same target/max rules. A comma/semicolon
    # fragment that is itself over the cap (no inner comma to split on) is
    # whitespace-split rather than failing the job. Preserves the original
    # separator (; vs ,) so XTTS-v2 prosody pauses match the cleanup output.
    chunks: list[str] = []
    current: list[tuple[str, str]] = []
    current_words = 0
    current_chars = 0

    def _flush_current() -> None:
        nonlocal current, current_words, current_chars
        if current:
            chunks.append(_join_pieces(current))
            current = []
            current_words = 0
            current_chars = 0

    for piece, sep in pieces_with_sep:
        p_words = len(piece.split())
        p_chars = len(piece)
        if p_words > limits.max_words or p_chars > limits.max_chars:
            # This fragment is over the cap on its own; flush what we have and
            # whitespace-split the fragment.
            _flush_current()
            chunks.extend(_fallback_split_on_whitespace(piece, limits, chunk_index))
            continue
        if current and (
            current_words + p_words > limits.target_words
            or current_chars + p_chars + 2 > limits.max_chars
        ):
            _flush_current()
        current.append((piece, sep))
        current_words += p_words
        current_chars += p_chars + (2 if current_chars else 0)
    _flush_current()
    return chunks


def _fallback_split_on_whitespace(
    sentence: str, limits: ChunkerLimits, chunk_index: int
) -> list[str]:
    """Last-resort split of a comma/semicolon-less oversize sentence on spaces.

    Greedy-packs words into pieces under the target word count and the absolute
    char cap. A whitespace split preserves every word -- it is not truncation --
    it only risks a less natural pause. A single whitespace-free token that is
    itself over the char cap is genuinely unsplittable and re-raises.
    """

    words = sentence.split()
    chunks: list[str] = []
    current: list[str] = []
    current_words = 0
    current_chars = 0

    for word in words:
        w_chars = len(word)
        if w_chars > limits.max_chars:
            # A lone token longer than the hard char cap can't be placed without
            # a mid-word cut; surface it rather than truncate.
            raise UnsplittableSentenceError(word, 1, w_chars)
        sep = 1 if current else 0
        if current and (
            current_words + 1 > limits.target_words
            or current_chars + sep + w_chars > limits.max_chars
        ):
            chunks.append(" ".join(current))
            current = []
            current_words = 0
            current_chars = 0
            sep = 0
        current.append(word)
        current_words += 1
        current_chars += sep + w_chars
    if current:
        chunks.append(" ".join(current))

    logger.warning(
        "Chunk fallback: whitespace split",
        extra={
            "event": "chunk_whitespace_split",
            "sentence_len_words": len(words),
            "sentence_len_chars": len(sentence),
            "chunk_index": chunk_index,
            "piece_count": len(chunks),
        },
    )
    return chunks


def _join_pieces(items: list[tuple[str, str]]) -> str:
    """Rebuild a sentence-fragment chunk preserving the original separators."""

    parts: list[str] = []
    for index, (piece, sep) in enumerate(items):
        parts.append(piece)
        if index < len(items) - 1:
            # The original separator was followed by whitespace in the source;
            # rebuild with ``;`` or ``,`` + space.
            parts.append(f"{sep} ")
    return "".join(parts)
