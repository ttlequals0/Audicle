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
