"""TTS engine abstraction so the FastAPI wrapper can be unit-tested without
importing the TTS model library or PyTorch.

``Engine`` is a Protocol the wrapper calls; ``ChatterboxEngine`` (in
``chatterbox_engine.py``) is the real implementation, with its heavy imports
deferred to ``load()`` so the module can be imported in environments without
``torch``. This module also holds the shared split/join/encode helpers and the
exceptions that engine reuses.
"""

from __future__ import annotations

import io
import logging
import re
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger("tts.engine")

# Most TTS models cap how much they synthesize per call (Chatterbox truncates long
# input). We split incoming text into pieces under a char budget and concatenate
# the audio, so the wrapper never 500s/truncates on a long chunk regardless of how
# the backend chunked it. The runtime cap comes from GenerationParams.max_chars;
# this constant is the fallback default.
_DEFAULT_MAX_CHARS = 300


@dataclass(frozen=True)
class GenerationParams:
    """Chatterbox generate knobs for one ``/generate`` call.

    The backend sends every field on each request -- its runtime settings are
    the single source since 0.44.0 (the wrapper's ``CHATTERBOX_*`` /
    ``TTS_MAX_CHARS`` env vars are gone). The defaults below only cover a
    request that omits a field (hand-curated curl). Only knobs the Turbo model
    honors are here -- it ignores CFG and exaggeration. temperature 0.5 (below
    Turbo's 0.8) trims sampling variance; repetition_penalty/top_p/top_k sit
    at the library defaults; seed makes a chunk reproducible (0 disables
    seeding); max_chars caps the sentence pieces fed to one inference call.
    """

    temperature: float = 0.5
    repetition_penalty: float = 1.2
    top_p: float = 0.95
    top_k: int = 1000
    seed: int = 1234
    max_chars: int = _DEFAULT_MAX_CHARS
    # Narration language for engines that support more than one; monolingual
    # engines only ever see "en" (the route 422s anything else).
    language: str = "en"


# Per-knob request bounds. main.py applies them to GenerateRequest so a bad
# value 422s instead of degrading audio silently; the backend mirrors them for
# PUT /settings validation (config.RUNTIME_SETTING_BOUNDS -- its drift test
# imports this module and pins the two tables together).
GENERATION_BOUNDS: dict[str, dict[str, float]] = {
    "temperature": {"gt": 0, "le": 2.0},
    "repetition_penalty": {"ge": 1.0, "le": 2.0},
    "top_p": {"gt": 0, "le": 1.0},
    "top_k": {"ge": 1, "le": 10000},
    "seed": {"ge": 0},
    "max_chars": {"ge": 100, "le": 2000},
}
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE = re.compile(r"\s+")
# Clause breaks to cut an oversize sentence on, so a join-gap lands on a natural pause
# rather than mid-clause: comma/semicolon/colon, or a spaced hyphen/em-dash.
_CLAUSE_BREAK = re.compile(r"[,;:]|\s--?\s|\s—\s")


def _cut_oversize(sentence: str, max_chars: int) -> tuple[str, str]:
    """Cut an oversize sentence into ``(head, rest)``. Prefer the rightmost clause break
    within the cap (keeping its punctuation on the head so the gap reads as a pause),
    then the rightmost word boundary, then a hard cut for a single oversize token."""

    window = sentence[:max_chars]
    cut = -1
    for match in _CLAUSE_BREAK.finditer(window):
        cut = match.end()
    if cut <= 0:
        cut = window.rfind(" ")
    if cut <= 0:
        cut = max_chars
    return sentence[:cut].strip(), sentence[cut:].strip()


def _split_into_pieces(text: str, max_chars: int = _DEFAULT_MAX_CHARS) -> list[str]:
    """Split ``text`` into pieces each <= ``max_chars``.

    Whitespace runs (including stray newlines, which Chatterbox reads as ~0.1s pauses)
    are collapsed to single spaces first. Sentence boundaries split next; an oversize
    sentence is cut at a clause boundary, falling back to a word boundary, then a hard
    cut only for a single token longer than the cap.
    """

    text = _WHITESPACE.sub(" ", text).strip()
    pieces: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(text):
        sentence = sentence.strip()
        while len(sentence) > max_chars:
            head, sentence = _cut_oversize(sentence, max_chars)
            pieces.append(head)
        if sentence:
            pieces.append(sentence)
    return pieces


# Piece-level edge-silence trim. Chatterbox Turbo's stop token is sampled, not
# enforced; when it fails to fire after the text completes, generation pads with
# silence to the 1000-token (~40 s) cap. Measured on one episode that dead air
# was 28.8% of all produced audio and drove most quality-gate failures, because
# the gate and whisper verify run on the raw chunk WAV. Trimming each piece at
# the source fixes every downstream consumer at once.
# Threshold mirrors the backend's AUDIO_SILENCE_THRESHOLD default. The buffer is
# larger than the backend's 5 ms edge trim because these cuts land next to the
# 0.12 s join gap and consonant tails must survive.
_TRIM_SILENCE_THRESHOLD = 0.003
_TRIM_BUFFER_MS = 50
# An all-silent piece keeps a short remnant instead of the full run: the text
# was supposed to be spoken, and a brief gap lets ASR verify flag the dropout
# (and regenerate) rather than the episode carrying the whole silent tail.
_ALL_SILENT_KEEP_MS = 250
# A degraded generation also strews multi-second pauses THROUGH the speech, not
# just after it (measured: flagged takes still 50-94% silent frames with edge
# trim active). Runs longer than MAX are cut to KEEP, half retained at each end
# so the speech decay/attack around the pause survives. Values mirror the
# backend's AUDIO_MAX_INTERNAL_SILENCE_MS / AUDIO_INTERNAL_SILENCE_KEEP_MS
# defaults, so this changes nothing audible -- the backend already applied the
# same compression to published audio, only after the quality gate had judged
# the uncompressed take.
_MAX_INTERNAL_SILENCE_MS = 1000
_INTERNAL_SILENCE_KEEP_MS = 500

# Generation-length cap. Chatterbox Turbo's stop token is sampled; a generation
# that misses it runs to the model's 1000-token (~40 s) limit at pure GPU cost.
# The backend gate rejects any take longer than (words / WORDS_PER_SEC +
# OVERHEAD) * MAX_RATIO seconds as overlong, so audio past that bound can never
# ship -- generating it is provable waste. Constants mirror the backend's
# AUDIO_ANALYSIS_* defaults; the overhead applies per piece where the backend
# applies it per chunk, which errs on the generous side.
_WORDS_PER_SEC = 2.7
_DURATION_OVERHEAD_SECS = 1.0
_MAX_DURATION_RATIO = 2.0
_S3_TOKENS_PER_SEC = 25
_MAX_GEN_TOKENS = 1000
_MIN_GEN_TOKENS = 125  # 5 s floor so a tiny piece is never starved


def max_gen_tokens(text: str) -> int:
    """Speech-token budget for one piece: the gate's overlong bound in tokens."""

    words = len(text.split())
    bound_secs = (words / _WORDS_PER_SEC + _DURATION_OVERHEAD_SECS) * _MAX_DURATION_RATIO
    return min(_MAX_GEN_TOKENS, max(_MIN_GEN_TOKENS, int(bound_secs * _S3_TOKENS_PER_SEC)))


def trim_edge_silence(wav, sample_rate: int):
    """Trim leading/trailing silence from a float32 mono piece, keeping a
    ``_TRIM_BUFFER_MS`` margin on each end. All-silent input returns a
    ``_ALL_SILENT_KEEP_MS`` remnant (never grows shorter input)."""

    import numpy as np  # noqa: PLC0415  (lazy: numpy comes from torch's wheel)

    above = np.abs(wav) > _TRIM_SILENCE_THRESHOLD
    indices = np.nonzero(above)[0]
    if indices.size == 0:
        # Slicing past the end is a no-op, so shorter input passes through.
        return wav[: int(sample_rate * _ALL_SILENT_KEEP_MS / 1000)]
    buffer_n = int(sample_rate * _TRIM_BUFFER_MS / 1000)
    start = max(0, int(indices[0]) - buffer_n)
    end = min(len(wav), int(indices[-1]) + 1 + buffer_n)
    return wav[start:end]


def compress_internal_silence_np(wav, sample_rate: int):
    """Shorten silence runs inside a piece that exceed ``_MAX_INTERNAL_SILENCE_MS``
    to ``_INTERNAL_SILENCE_KEEP_MS`` (half kept at each end of the run). numpy
    mirror of the backend's ``compress_internal_silence``; an all-silent piece is
    returned unchanged (``trim_edge_silence`` owns that case)."""

    import numpy as np  # noqa: PLC0415  (lazy: numpy comes from torch's wheel)

    silent = np.abs(wav) <= _TRIM_SILENCE_THRESHOLD
    if silent.all() or not silent.any():
        return wav

    max_run = int(sample_rate * _MAX_INTERNAL_SILENCE_MS / 1000)
    keep_half = int(sample_rate * _INTERNAL_SILENCE_KEEP_MS / 2000)

    edges = np.diff(silent.astype(np.int8))
    starts = (np.nonzero(edges == 1)[0] + 1).tolist()
    ends = (np.nonzero(edges == -1)[0] + 1).tolist()
    if silent[0]:
        starts.insert(0, 0)
    if silent[-1]:
        ends.append(len(wav))

    pieces = []
    cursor = 0
    for start, end in zip(starts, ends, strict=True):
        if end - start <= max_run:
            continue
        pieces.append(wav[cursor : start + keep_half])
        cursor = end - keep_half
    if not pieces:  # no run exceeded the cap
        return wav
    pieces.append(wav[cursor:])
    return np.concatenate(pieces)


def pcm16_wav_bytes(wav_array, sample_rate: int) -> bytes:
    """Encode a 1D float32 array in [-1, 1] as 16-bit PCM mono WAV bytes.

    Shared by every engine so the on-disk WAV format is identical regardless of
    which model produced the samples.
    """

    import numpy as np  # noqa: PLC0415  (lazy: numpy comes from torch's wheel)

    clamped = np.clip(wav_array, -1.0, 1.0)
    int16 = (clamped * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(int16.tobytes())
    return buf.getvalue()


def join_with_silence(wavs, sample_rate: int, gap_secs: float = 0.12):
    """Concatenate float32 audio pieces separated by a short silence gap.

    Assumes a non-empty list (callers return silence for empty text first). The
    gap keeps sentence pieces from slurring together when a chunk was split.
    """

    import numpy as np  # noqa: PLC0415  (lazy: numpy comes from torch's wheel)

    if len(wavs) == 1:
        return wavs[0]
    gap = np.zeros(int(sample_rate * gap_secs), dtype=np.float32)
    joined: list = [wavs[0]]
    for wav in wavs[1:]:
        joined += [gap, wav]
    return np.concatenate(joined)


class GPUOutOfMemoryError(RuntimeError):
    """Raised when CUDA OOMs mid-synthesis.

    Subclasses ``RuntimeError`` rather than importing torch's exception so the
    wrapper's HTTP layer can catch it without forcing a torch import.
    """


class InferenceBusyError(RuntimeError):
    """Raised when a /generate arrives while a prior inference is still running.

    ``asyncio.wait_for`` cancels the awaiting coroutine on timeout but cannot
    cancel the OS thread running torch inference, so the GPU work continues after
    the route has returned 504 and released its lock. The backend retries the
    504, and without this guard each retry would spawn another concurrent
    inference thread and exhaust VRAM. The wrapper rejects the overlapping call
    with 503 instead; the orphaned thread finishes and frees the GPU on its own.
    """


@runtime_checkable
class Engine(Protocol):
    """Minimal contract the FastAPI wrapper depends on."""

    model_loaded: bool
    reference_loaded: bool
    sample_rate: int
    device: str
    name: str  # "chatterbox"
    languages: tuple[str, ...]  # language ids /generate may carry

    def load(self) -> None:
        """Synchronous startup: load model weights + reference embeddings.

        Raises if the reference WAV is missing or the model can't be loaded.
        The wrapper's lifespan re-raises so uvicorn exits non-zero (container
        restart loop surfaces the misconfig instead of serving 500s).
        """

    async def synthesize(self, text: str, params: GenerationParams) -> bytes:
        """Return a WAV byte string for ``text``.

        ``params`` carries the per-request generation knobs (the /generate
        route builds it, filling omitted fields with the defaults). Raises
        :class:`GPUOutOfMemoryError` on CUDA OOM and
        :class:`InferenceBusyError` when another inference is already running
        (mapped to 503). Any other exception propagates as a 500 to the client.
        """

    async def reload_reference(self) -> None:
        """Re-read the reference WAV from disk and recompute embeddings.

        Used by the API's ``/reload`` endpoint after a reference-voice commit.
        """

    async def select_voice(self, ref_path: Path) -> None:
        """Encode a specific reference clip (a voice slot) as the active voice.

        Used by the API's ``/select-voice`` endpoint to switch the per-job voice.
        """
