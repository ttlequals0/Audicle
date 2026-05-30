"""TTS engine abstraction so the FastAPI wrapper can be unit-tested without
importing Coqui TTS or PyTorch.

``Engine`` is a Protocol the wrapper calls. ``XTTSEngine`` is the real
implementation; its imports are deferred to :meth:`XTTSEngine.load` so the
module can be imported in environments without ``torch``.
"""

from __future__ import annotations

import io
import logging
import re
import time
import wave
from pathlib import Path
from typing import Protocol, runtime_checkable

from config import Config

logger = logging.getLogger("tts.engine")

# XTTS-v2 can only synthesize ~400 tokens per inference() call and its tokenizer
# warns above 250 chars for English. We split incoming text into pieces under
# this budget and concatenate the audio, so the wrapper never 500s on a long
# chunk regardless of how the backend chunked it.
_XTTS_MAX_CHARS = 240
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _split_for_xtts(text: str, max_chars: int = _XTTS_MAX_CHARS) -> list[str]:
    """Split ``text`` into pieces each <= ``max_chars`` (XTTS-safe).

    Sentence boundaries first; an oversize sentence is cut at the last space
    before the cap (a hard cut only if a single token exceeds the cap).
    """

    pieces: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(text.strip()):
        sentence = sentence.strip()
        while len(sentence) > max_chars:
            cut = sentence.rfind(" ", 0, max_chars)
            if cut <= 0:
                cut = max_chars
            head, sentence = sentence[:cut].strip(), sentence[cut:].strip()
            if head:
                pieces.append(head)
        if sentence:
            pieces.append(sentence)
    return pieces


class GPUOutOfMemoryError(RuntimeError):
    """Raised when CUDA OOMs mid-synthesis.

    Subclasses ``RuntimeError`` rather than importing torch's exception so the
    wrapper's HTTP layer can catch it without forcing a torch import.
    """


@runtime_checkable
class Engine(Protocol):
    """Minimal contract the FastAPI wrapper depends on."""

    model_loaded: bool
    reference_loaded: bool
    sample_rate: int
    device: str

    def load(self) -> None:
        """Synchronous startup: load model weights + reference embeddings.

        Raises if the reference WAV is missing or the model can't be loaded.
        The wrapper's lifespan re-raises so uvicorn exits non-zero (container
        restart loop surfaces the misconfig instead of serving 500s).
        """

    async def synthesize(self, text: str) -> bytes:
        """Return a WAV byte string for ``text``.

        Raises :class:`GPUOutOfMemoryError` on CUDA OOM. Any other exception
        propagates as a 500 to the client.
        """

    async def reload_reference(self) -> None:
        """Re-read the reference WAV from disk and recompute embeddings.

        Used by the API's ``/reload`` endpoint after a reference-voice commit.
        """


class XTTSEngine:
    """Real Coqui TTS XTTS-v2 backend.

    Lazy imports of ``torch`` and ``TTS`` so this module is importable in
    test environments without GPU runtime. Embeddings are cached on the
    instance; ``synthesize`` reuses them across every call.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.model_loaded = False
        self.reference_loaded = False
        self.sample_rate = config.sample_rate
        self.device = config.device
        self._model = None
        self._gpt_cond_latent = None
        self._speaker_embedding = None
        self._torch = None  # cached torch module reference

    def load(self) -> None:
        import torch  # noqa: PLC0415  (intentional lazy import)
        from TTS.api import TTS  # noqa: PLC0415

        self._torch = torch
        # torch 2.6 flipped torch.load's default to weights_only=True, which
        # refuses to unpickle XTTS's custom config classes and breaks model
        # loading. The checkpoint is the trusted, bundled XTTS-v2 model (not user
        # input), so allowlist its config classes as safe globals rather than
        # forcing weights_only=False. Best-effort: tolerate coqui-tts layouts
        # that don't expose every class.
        self._register_xtts_safe_globals(torch)
        device = self.config.device
        logger.info("Loading XTTS-v2 model", extra={"event": "tts_model_loading", "device": device})

        # progress_bar=False keeps the container log clean.
        load_started = time.perf_counter()
        self._model = TTS("tts_models/multilingual/multi-dataset/xtts_v2", progress_bar=False).to(
            device
        )
        self.model_loaded = True
        logger.info(
            "XTTS-v2 model loaded",
            extra={
                "event": "tts_model_loaded",
                "device": device,
                "load_ms": int((time.perf_counter() - load_started) * 1000),
            },
        )

        # The reference voice is optional at startup: the operator can upload one
        # later via the UI (which writes voice.wav and calls /reload). Compute
        # embeddings now if a usable clip is present; a missing OR unreadable one
        # just leaves reference_loaded=false and /generate returning 503 -- the
        # wrapper stays up so the operator can upload a good clip rather than
        # crash-looping on a bad pre-staged file.
        ref_path = Path(self.config.reference_path)
        if not ref_path.exists():
            logger.warning(
                "No reference voice yet; upload one via the UI. /generate is "
                "unavailable until a voice is committed.",
                extra={"event": "tts_reference_missing", "path": str(ref_path)},
            )
            return
        try:
            self._compute_embeddings(ref_path)
        except Exception:
            logger.warning(
                "Reference voice present but could not be decoded; ignoring it. "
                "Upload a valid clip via the UI. /generate is unavailable until a "
                "usable voice is committed.",
                extra={"event": "tts_reference_invalid", "path": str(ref_path)},
                exc_info=True,
            )

    @staticmethod
    def _register_xtts_safe_globals(torch) -> None:
        """Allowlist XTTS config classes for torch 2.6's weights_only loader.

        Imports are best-effort: coqui-tts versions differ in where these live,
        and on torch < 2.6 ``add_safe_globals`` may be absent -- a missing class
        or API just means we skip it (older torch ignores weights_only anyway).
        """

        add_safe_globals = getattr(torch.serialization, "add_safe_globals", None)
        if add_safe_globals is None:
            return
        safe: list[type] = []
        try:
            from TTS.config.shared_configs import BaseDatasetConfig  # noqa: PLC0415
            from TTS.tts.configs.xtts_config import XttsConfig  # noqa: PLC0415
            from TTS.tts.models.xtts import XttsArgs, XttsAudioConfig  # noqa: PLC0415

            safe = [XttsConfig, XttsAudioConfig, XttsArgs, BaseDatasetConfig]
        except ImportError:
            logger.warning(
                "Could not import all XTTS config classes for safe-globals; "
                "model load may need weights_only handling.",
                extra={"event": "tts_safe_globals_partial"},
            )
        if safe:
            add_safe_globals(safe)

    def _compute_embeddings(self, ref_path: Path) -> None:
        assert self._model is not None
        logger.info(
            "Computing speaker embeddings",
            extra={"event": "tts_embeddings_compute", "reference": str(ref_path)},
        )
        gpt_cond_latent, speaker_embedding = (
            self._model.synthesizer.tts_model.get_conditioning_latents(audio_path=[str(ref_path)])
        )
        self._gpt_cond_latent = gpt_cond_latent
        self._speaker_embedding = speaker_embedding
        self.reference_loaded = True

    async def reload_reference(self) -> None:
        import asyncio  # noqa: PLC0415

        ref_path = Path(self.config.reference_path)
        if not ref_path.exists():
            raise FileNotFoundError(f"reference voice not found at {ref_path}")
        # Snapshot existing state so a failed recompute doesn't leave the
        # wrapper permanently reporting reference_loaded=false on /health.
        previous_latent = self._gpt_cond_latent
        previous_speaker = self._speaker_embedding
        previous_loaded = self.reference_loaded
        self.reference_loaded = False
        try:
            await asyncio.to_thread(self._compute_embeddings, ref_path)
        except Exception:
            # Roll back to the prior good state. Operator gets a 5xx from
            # /reload, /health stays accurate, the wrapper keeps serving the
            # old voice until the next successful reload.
            self._gpt_cond_latent = previous_latent
            self._speaker_embedding = previous_speaker
            self.reference_loaded = previous_loaded
            raise

    async def synthesize(self, text: str) -> bytes:
        import asyncio  # noqa: PLC0415

        assert self._model is not None
        assert self._torch is not None
        try:
            # Offload the blocking torch inference to a worker thread so the
            # event loop can still service /health probes while a chunk is in
            # flight. asyncio.Lock in the route still serializes GPU access.
            wav_array = await asyncio.to_thread(self._run_inference, text)
        except self._torch.cuda.OutOfMemoryError as exc:
            # Free fragmented cache so subsequent calls have a chance.
            self._torch.cuda.empty_cache()
            raise GPUOutOfMemoryError(str(exc)) from exc

        return self._wav_bytes(wav_array)

    def _run_inference(self, text: str):
        import numpy as np  # noqa: PLC0415  (lazy: numpy comes from torch's wheel)

        assert self._model is not None
        # _split_for_xtts only returns non-empty pieces; an empty list means the
        # text had no speakable content (e.g. a whitespace-only chunk). Return a
        # short silence instead of feeding "" to XTTS, which crashes inference.
        pieces = _split_for_xtts(text)
        if not pieces:
            return np.zeros(int(self.sample_rate * 0.05), dtype=np.float32)
        wavs = [np.asarray(self._infer_piece(piece), dtype=np.float32) for piece in pieces]
        if len(wavs) == 1:
            return wavs[0]
        # Join sentence pieces with a short silence so they don't slur together.
        gap = np.zeros(int(self.sample_rate * 0.12), dtype=np.float32)
        joined: list = [wavs[0]]
        for wav in wavs[1:]:
            joined += [gap, wav]
        return np.concatenate(joined)

    def _infer_piece(self, text: str):
        assert self._model is not None
        return self._model.synthesizer.tts_model.inference(
            text=text,
            language=self.config.language,
            gpt_cond_latent=self._gpt_cond_latent,
            speaker_embedding=self._speaker_embedding,
            temperature=self.config.temperature,
            length_penalty=self.config.length_penalty,
            repetition_penalty=self.config.repetition_penalty,
            top_k=self.config.top_k,
            top_p=self.config.top_p,
        )["wav"]

    def _wav_bytes(self, wav_array) -> bytes:
        """Encode a 1D float32 array as WAV bytes at the configured sample rate."""

        import numpy as np  # noqa: PLC0415  (lazy: numpy comes from torch's wheel)

        # XTTS returns float in [-1, 1]; convert to int16 PCM.
        clamped = np.clip(wav_array, -1.0, 1.0)
        int16 = (clamped * 32767.0).astype(np.int16)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(self.sample_rate)
            wf.writeframes(int16.tobytes())
        return buf.getvalue()
