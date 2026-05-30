"""Hybrid text chunker.

Order of fallback when a paragraph doesn't fit:

1. Split on paragraph boundaries (``\\n\\n``). Greedy-pack into chunks of
   ``TTS_CHUNK_TARGET_WORDS`` words.
2. If a single sentence (or paragraph treated as one) exceeds the target,
   split it on sentence boundaries.
3. If a sentence still exceeds the max, fall back to comma / semicolon
   splits and emit a WARN log so an article with many such cases is
   visible in Loki (event=chunk_fallback_split).
4. If even comma/semicolon splits can't fit a piece under
   ``TTS_CHUNK_MAX_WORDS``/``TTS_CHUNK_MAX_CHARS``, raise
   :class:`UnsplittableSentenceError` so the pipeline marks the job
   ``failed`` with stage=chunk and a sentence-preview error rather than
   silently truncating content.
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
    word_count = len(paragraph.split())
    char_count = len(paragraph)
    if word_count <= limits.target_words and char_count <= limits.max_chars:
        return [paragraph]
    # Paragraph too big; switch to sentence-level packing.
    sentences = _split_sentences(paragraph)
    return _pack(sentences, limits, base_chunk_index=base_chunk_index)


def _split_sentences(paragraph: str) -> list[str]:
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
        # No comma / semicolon found, or only one effective piece -- nothing
        # we can do without forcing a mid-word split or truncating content.
        raise UnsplittableSentenceError(sentence, len(sentence.split()), len(sentence))

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

    # Re-pack the pieces under the same target/max rules. A piece that's
    # itself oversize is genuinely unsplittable -- raise rather than truncate.
    # Preserves the original separator (; vs ,) so XTTS-v2 prosody pauses
    # match what the cleanup stage produced.
    chunks: list[str] = []
    current: list[tuple[str, str]] = []
    current_words = 0
    current_chars = 0

    for piece, sep in pieces_with_sep:
        p_words = len(piece.split())
        p_chars = len(piece)
        if p_words > limits.max_words or p_chars > limits.max_chars:
            raise UnsplittableSentenceError(piece, p_words, p_chars)
        if current and (
            current_words + p_words > limits.target_words
            or current_chars + p_chars + 2 > limits.max_chars
        ):
            chunks.append(_join_pieces(current))
            current = []
            current_words = 0
            current_chars = 0
        current.append((piece, sep))
        current_words += p_words
        current_chars += p_chars + (2 if current_chars else 0)
    if current:
        chunks.append(_join_pieces(current))
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
