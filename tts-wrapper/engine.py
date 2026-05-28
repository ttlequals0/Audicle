"""TTS engine abstraction so the FastAPI wrapper can be unit-tested without
importing Coqui TTS or PyTorch.

``Engine`` is a Protocol the wrapper calls. ``XTTSEngine`` is the real
implementation; its imports are deferred to :meth:`XTTSEngine.load` so the
module can be imported in environments without ``torch``.
"""

from __future__ import annotations

import io
import logging
import wave
from pathlib import Path
from typing import Protocol, runtime_checkable

from config import Config

logger = logging.getLogger("tts.engine")


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
        self._model = None
        self._gpt_cond_latent = None
        self._speaker_embedding = None
        self._torch = None  # cached torch module reference

    def load(self) -> None:
        import torch  # noqa: PLC0415  (intentional lazy import)
        from TTS.api import TTS  # noqa: PLC0415

        self._torch = torch
        device = self.config.device
        logger.info("Loading XTTS-v2 model", extra={"event": "tts_model_loading", "device": device})

        ref_path = Path(self.config.reference_path)
        if not ref_path.exists():
            raise FileNotFoundError(
                f"reference voice not found at {ref_path}; mount voice.wav to that path"
            )

        # ``progress_bar=False`` keeps the container log clean.
        self._model = TTS("tts_models/multilingual/multi-dataset/xtts_v2", progress_bar=False).to(
            device
        )
        self.model_loaded = True

        self._compute_embeddings(ref_path)

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
