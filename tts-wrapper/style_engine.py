"""StyleTTS2 engine: zero-shot voice cloning with IPA phoneme injection.

A sibling of :class:`engine.XTTSEngine` behind the same :class:`engine.Engine`
Protocol, selected with ``TTS_ENGINE=styletts2``. Unlike XTTS (text-only), this
engine phonemizes with gruut and can splice operator-supplied IPA into the phoneme
stream, so curated pronunciations (CMUdict/ISLEX/Wiktionary) are honored exactly.

The single-flight ``_gpu_lock``, OOM mapping, and ``reload_reference`` rollback
mirror XTTSEngine so the wrapper's 503/500/504 + ``/reload`` behavior is identical.

NOTE: validated only on the GPU host (the B0 spike). The exact phoneme-list entry
point of the ``styletts2`` package is confirmed during that spike; until then the
inference call here follows the package's documented text/phoneme API and is gated
behind the engine flag (XTTS stays the default).
"""

from __future__ import annotations

import io
import logging
import re
import threading
import wave
from pathlib import Path

from config import Config
from engine import GPUOutOfMemoryError, InferenceBusyError

logger = logging.getLogger("tts.style_engine")

# Same sentence-splitting budget as XTTS so long chunks don't exceed the model's
# per-inference window.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def inject_phonemes(text: str, pronunciations: dict[str, str] | None, phonemize):
    """Build a phoneme string for ``text``, splicing curated IPA for override terms.

    ``phonemize`` is ``(plain_text) -> ipa`` (gruut at runtime; a stub in tests).
    Override terms (whole-word, longest-first) are emitted as their supplied IPA
    verbatim; the spans between them are phonemized normally. Pure and testable
    without gruut or the model.
    """

    if not pronunciations:
        return phonemize(text)
    terms = sorted((t for t in pronunciations if t), key=len, reverse=True)
    if not terms:  # all keys empty/falsy -> nothing to splice (avoid an empty-group regex)
        return phonemize(text)
    pattern = re.compile(r"(?<![\w-])(" + "|".join(re.escape(t) for t in terms) + r")(?![\w-])")
    out: list[str] = []
    pos = 0
    for match in pattern.finditer(text):
        if match.start() > pos:
            chunk = text[pos : match.start()].strip()
            if chunk:
                out.append(phonemize(chunk))
        out.append(pronunciations[match.group(1)])  # curated IPA verbatim
        pos = match.end()
    tail = text[pos:].strip()
    if tail:
        out.append(phonemize(tail))
    return " ".join(p for p in out if p)


class StyleTTS2Engine:
    """StyleTTS2 backend with gruut phonemization and IPA injection."""

    name = "styletts2"
    supports_phonemes = True

    def __init__(self, config: Config) -> None:
        self.config = config
        self.model_loaded = False
        self.reference_loaded = False
        self.sample_rate = config.sample_rate
        self.device = config.device
        self._model = None
        self._torch = None
        self._gruut = None
        self._infer_shape: str | None = None  # cached working inference signature
        self._gpu_lock = threading.Lock()

    def load(self) -> None:
        import torch  # noqa: PLC0415
        from gruut import sentences  # noqa: PLC0415
        from styletts2 import tts  # noqa: PLC0415

        self._torch = torch
        self._gruut = sentences
        model_path = self.config.style_model_path or None
        logger.info(
            "Loading StyleTTS2 model",
            extra={"event": "style_model_loading", "device": self.device, "path": model_path},
        )
        # The package downloads bundled weights when model_path is None.
        self._model = tts.StyleTTS2(model_checkpoint_path=model_path) if model_path else tts.StyleTTS2()
        self.model_loaded = True
        ref = Path(self.config.reference_path)
        self.reference_loaded = ref.exists()
        if not self.reference_loaded:
            logger.warning(
                "No reference voice yet; /generate unavailable until one is committed.",
                extra={"event": "style_reference_missing", "path": str(ref)},
            )

    async def reload_reference(self) -> None:
        # StyleTTS2 clones from the reference WAV at inference time (no precomputed
        # embedding), so a reload is just re-checking the file exists.
        ref = Path(self.config.reference_path)
        if not ref.exists():
            raise FileNotFoundError(f"reference voice not found at {ref}")
        self.reference_loaded = True

    def _phonemize(self, text: str) -> str:
        assert self._gruut is not None
        phones: list[str] = []
        for sent in self._gruut(text, lang=self.config.style_phonemizer_lang):
            for word in sent:
                if word.phonemes:
                    phones.append("".join(word.phonemes))
        return " ".join(phones)

    async def synthesize(self, text: str, pronunciations: dict[str, str] | None = None) -> bytes:
        import asyncio  # noqa: PLC0415

        assert self._model is not None
        assert self._torch is not None
        try:
            wav_array = await asyncio.to_thread(self._run_inference, text, pronunciations)
        except self._torch.cuda.OutOfMemoryError as exc:
            self._torch.cuda.empty_cache()
            raise GPUOutOfMemoryError(str(exc)) from exc
        return self._wav_bytes(wav_array)

    def _run_inference(self, text: str, pronunciations: dict[str, str] | None):
        import numpy as np  # noqa: PLC0415

        assert self._model is not None
        if not self._gpu_lock.acquire(blocking=False):
            raise InferenceBusyError("an inference is already running on this wrapper")
        try:
            pieces = [p.strip() for p in _SENTENCE_SPLIT.split(text.strip()) if p.strip()]
            if not pieces:
                return np.zeros(int(self.sample_rate * 0.05), dtype=np.float32)
            wavs = []
            for piece in pieces:
                phonemes = inject_phonemes(piece, pronunciations, self._phonemize)
                wavs.append(np.asarray(self._infer_piece(phonemes), dtype=np.float32))
            if len(wavs) == 1:
                return wavs[0]
            gap = np.zeros(int(self.sample_rate * 0.12), dtype=np.float32)
            joined: list = [wavs[0]]
            for wav in wavs[1:]:
                joined += [gap, wav]
            return np.concatenate(joined)
        finally:
            self._gpu_lock.release()

    def _call_shape(self, shape: str, phonemes: str):
        ref = self.config.reference_path
        if shape == "kw_phonemes":
            return self._model.inference(phonemes=phonemes, target_voice_path=ref)
        if shape == "kw_text":
            return self._model.inference(text=phonemes, target_voice_path=ref)
        return self._model.inference(phonemes, target_voice_path=ref)  # positional

    def _infer_piece(self, phonemes: str):
        assert self._model is not None
        # The styletts2 package's inference signature isn't validated outside the GPU
        # spike, so try the likely call shapes in order and cache the SHAPE NAME (not a
        # bound call -- that would freeze the first phonemes) that works. The injected
        # IPA is passed as the spoken content for whichever kwarg the package accepts.
        if self._infer_shape is not None:
            return self._call_shape(self._infer_shape, phonemes)
        last_exc: Exception | None = None
        for shape in ("kw_phonemes", "kw_text", "positional"):
            try:
                out = self._call_shape(shape, phonemes)
            except TypeError as exc:  # wrong signature -> try the next shape
                last_exc = exc
                continue
            self._infer_shape = shape
            logger.info(
                "StyleTTS2 inference shape resolved",
                extra={"event": "style_infer_shape", "shape": shape},
            )
            return out
        raise RuntimeError(f"no styletts2 inference signature matched: {last_exc}")

    def _wav_bytes(self, wav_array) -> bytes:
        import numpy as np  # noqa: PLC0415

        clamped = np.clip(wav_array, -1.0, 1.0)
        int16 = (clamped * 32767.0).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(int16.tobytes())
        return buf.getvalue()
