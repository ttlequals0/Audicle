"""Chatterbox (Turbo) engine: zero-shot voice cloning via Resemble AI's
``chatterbox-tts`` library.

The wrapper's TTS engine, behind the :class:`engine.Engine` Protocol. Text-only
(no IPA phoneme injection); text-level lexicon corrections apply upstream in the
pipeline.

The reference voice is encoded once into the model's conditionals
(``prepare_conditionals``) at load / ``/reload`` and reused for every chunk, so
synthesis does not re-read the reference per call. The single-flight
``_gpu_lock``, OOM mapping, and ``reload_reference`` rollback give the wrapper a
consistent 503/500/504 + ``/reload`` behavior.

Imports of ``torch`` and ``chatterbox`` are deferred to :meth:`load` so this
module is importable in test environments without the GPU runtime.

NOTE: the backend sends the sampling knobs (temperature, repetition_penalty,
top_p, top_k, seed, max_chars) on every /generate call
(``engine.GenerationParams`` -- the env knobs were removed in 0.44.0). Turbo's
``generate()`` ignores CFG and exaggeration, so those are not exposed;
exaggeration is baked into the reference conditionals at 0.0 (neutral read).
Every output carries Resemble's inaudible PerTh watermark (no disable flag in
the library).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from config import NUM_SLOTS, Config
from engine import (
    GenerationParams,
    GPUOutOfMemoryError,
    InferenceBusyError,
    _split_into_pieces,
    join_with_silence,
    pcm16_wav_bytes,
)

logger = logging.getLogger("tts.chatterbox")


class ChatterboxEngine:
    """Chatterbox Turbo backend with cached reference conditionals."""

    name = "chatterbox"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.model_loaded = False
        self.reference_loaded = False
        # Provisional rate; replaced with the model's own ``sr`` after load.
        self.sample_rate = config.sample_rate
        self.device = config.device
        self._model = None
        self._torch = None  # cached torch module reference
        # The reference clip whose conditionals are currently encoded. Lets
        # ``select_voice`` skip a redundant re-encode when the requested voice is
        # already active -- the backend re-selects the job voice before every chunk to
        # survive a concurrent audition, and without this that would re-encode each time.
        self._current_ref: Path | None = None
        # Single-flight guard over ALL GPU work (inference and conditional
        # recompute); an overlapping call is rejected rather than run concurrently.
        self._gpu_lock = threading.Lock()

    def load(self) -> None:
        import torch  # noqa: PLC0415  (intentional lazy import)
        from chatterbox.tts_turbo import ChatterboxTurboTTS  # noqa: PLC0415

        self._torch = torch
        device = self.config.device
        logger.info(
            "Loading Chatterbox Turbo model",
            extra={"event": "tts_model_loading", "device": device},
        )
        load_started = time.perf_counter()
        self._model = ChatterboxTurboTTS.from_pretrained(device=device)
        # Trust the model's native output rate (24 kHz) over the config default.
        self.sample_rate = int(self._model.sr)
        self.model_loaded = True
        logger.info(
            "Chatterbox Turbo model loaded",
            extra={
                "event": "tts_model_loaded",
                "device": device,
                "sample_rate": self.sample_rate,
                "load_ms": int((time.perf_counter() - load_started) * 1000),
            },
        )

        # Reference voice is optional at startup (operator uploads one to a slot via
        # the UI/API). Slots-only model: the resting voice is the lowest filled slot,
        # not a separate committed voice.wav. No slots yet just leaves
        # reference_loaded=false and /generate returning 503 -- the wrapper stays up
        # so the operator can upload a clip.
        ref_path = self._boot_reference_path()
        if ref_path is None:
            logger.warning(
                "No voice slots yet; upload one via the UI. /generate is "
                "unavailable until a voice is added.",
                extra={"event": "tts_reference_missing"},
            )
            return
        try:
            self._prepare_reference(ref_path)
        except Exception:
            logger.warning(
                "Reference voice present but could not be decoded; ignoring it. "
                "Upload a valid clip via the UI. /generate is unavailable until a "
                "usable voice is committed.",
                extra={"event": "tts_reference_invalid", "path": str(ref_path)},
                exc_info=True,
            )

    def _prepare_reference(self, ref_path: Path) -> None:
        assert self._model is not None
        # Encoding the reference is GPU work too: reject if an inference (possibly
        # an orphaned post-timeout thread) is still on the device, so /reload
        # can't run concurrently with it. Uncontended at startup (load()).
        if not self._gpu_lock.acquire(blocking=False):
            raise InferenceBusyError("an inference is already running on this wrapper")
        try:
            logger.info(
                "Encoding reference conditionals",
                extra={"event": "tts_embeddings_compute", "reference": str(ref_path)},
            )
            # exaggeration exists only here: it is baked into the conditionals'
            # emotion tensor at prepare time, and Turbo's generate() ignores a
            # per-call value ("CFG, min_p and exaggeration are not supported by
            # Turbo version"). Pin the neutral 0.0 (the library default is 0.5)
            # rather than expose a knob the model would ignore.
            self._model.prepare_conditionals(str(ref_path), exaggeration=0.0)
            self.reference_loaded = True
            self._current_ref = ref_path
        finally:
            self._gpu_lock.release()

    async def _swap_reference(self, ref_path: Path) -> None:
        import asyncio  # noqa: PLC0415

        if not ref_path.exists():
            raise FileNotFoundError(f"reference voice not found at {ref_path}")
        # prepare_conditionals only swaps model.conds on success, so a failed
        # recompute leaves the prior voice in place; we just roll the flag back.
        previous_loaded = self.reference_loaded
        self.reference_loaded = False
        try:
            await asyncio.to_thread(self._prepare_reference, ref_path)
        except Exception:
            self.reference_loaded = previous_loaded
            raise

    def _boot_reference_path(self) -> Path | None:
        """The wrapper's resting voice: the lowest-numbered filled slot, or None when
        no slots exist. Slots-only model -- there is no separate committed voice.wav.
        Uses ``Config.slot_path`` (the same layout /select-voice uses). Keep the
        lowest-filled rule in lockstep with the backend's ``voices.default_slot`` so a
        no-voice job's audio and its recorded label resolve to the same slot."""

        for slot in range(1, NUM_SLOTS + 1):
            candidate = self.config.slot_path(slot)
            if candidate.exists():
                return candidate
        return None

    async def reload_reference(self) -> None:
        """Re-encode the resting voice (lowest filled slot). No-op when no slots
        exist, so a /reload on an empty wrapper doesn't raise."""

        ref_path = self._boot_reference_path()
        if ref_path is not None:
            await self._swap_reference(ref_path)

    async def select_voice(self, ref_path: Path) -> None:
        """Encode a specific reference clip (a voice slot) for the next job. Same
        rollback semantics as /reload -- a failed encode keeps the prior voice.

        Idempotent: if the requested clip is already the active voice, skip the
        re-encode. The backend re-selects the job voice before every chunk to survive a
        concurrent audition, so the no-op path keeps that cheap."""

        if self.reference_loaded and self._current_ref == ref_path:
            return
        await self._swap_reference(ref_path)

    async def synthesize(self, text: str, params: GenerationParams) -> bytes:
        import asyncio  # noqa: PLC0415

        assert self._model is not None
        assert self._torch is not None
        try:
            wav_array = await asyncio.to_thread(self._run_inference, text, params)
        except self._torch.cuda.OutOfMemoryError as exc:
            self._torch.cuda.empty_cache()
            raise GPUOutOfMemoryError(str(exc)) from exc
        return pcm16_wav_bytes(wav_array, self.sample_rate)

    def _run_inference(self, text: str, params: GenerationParams):
        import numpy as np  # noqa: PLC0415  (lazy: numpy comes from torch's wheel)

        assert self._model is not None
        # Reject an overlapping GPU operation rather than running it concurrently.
        if not self._gpu_lock.acquire(blocking=False):
            raise InferenceBusyError("an inference is already running on this wrapper")
        try:
            # Chatterbox truncates long input rather than chunking, so split into
            # speakable pieces under the cap and concatenate.
            pieces = _split_into_pieces(text, params.max_chars)
            if not pieces:
                return np.zeros(int(self.sample_rate * 0.05), dtype=np.float32)
            # Seed before generating so a chunk is reproducible run-to-run; this
            # plus the lower temperature is what steadies pronunciation. The
            # backend sends its baseline seed on attempt 0 and a distinct seed on
            # a quality regeneration so the re-gen produces *different* audio.
            if params.seed != 0:
                self._set_seed(params.seed)
            wavs = [
                np.asarray(self._infer_piece(piece, params), dtype=np.float32)
                for piece in pieces
            ]
            return join_with_silence(wavs, self.sample_rate)
        finally:
            self._gpu_lock.release()

    def _set_seed(self, seed: int) -> None:
        import random  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415  (lazy: numpy comes from torch's wheel)

        assert self._torch is not None
        self._torch.manual_seed(seed)
        if self._torch.cuda.is_available():
            self._torch.cuda.manual_seed_all(seed)
        # np.random.seed rejects values outside [0, 2**32-1]; mask so an
        # operator-set seed can't crash inference.
        np.random.seed(seed & 0xFFFFFFFF)
        random.seed(seed)

    def _infer_piece(self, text: str, params: GenerationParams):
        assert self._model is not None
        # No audio_prompt_path -> reuse the conditionals cached by
        # prepare_conditionals (the per-chunk performance win). Only knobs
        # Turbo honors are passed; exaggeration/cfg_weight would be ignored
        # with a per-piece library warning.
        wav = self._model.generate(
            text,
            temperature=params.temperature,
            repetition_penalty=params.repetition_penalty,
            top_p=params.top_p,
            top_k=params.top_k,
        )
        # generate returns a torch.Tensor (1, N) float32 in [-1, 1].
        return wav.squeeze(0).detach().cpu().numpy()
