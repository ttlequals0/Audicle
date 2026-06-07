"""Optional faster-whisper transcription for post-TTS verification.

The model is loaded lazily on first use so the wrapper image and the test
environment never need faster-whisper unless WHISPER_ENABLED is set. The
backend diffs the returned transcript against the text it asked the wrapper to
speak; this module only produces a blind transcript (no expected-text prompt,
so the diff stays meaningful) and never decides quality itself.
"""

from __future__ import annotations

import io
import logging
import threading

logger = logging.getLogger("tts.whisper")


class WhisperVerifier:
    """Lazy faster-whisper wrapper. Thread-safe single load."""

    def __init__(self, model_name: str, device: str, compute_type: str) -> None:
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._model = None
        self._load_lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Eagerly load the model (called once at startup so the multi-GB
        download/init does not land inside the first /generate request)."""

        self._ensure_model()

    def _ensure_model(self):
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    from faster_whisper import WhisperModel  # lazy: heavy dep

                    logger.info(
                        "Loading faster-whisper model",
                        extra={
                            "event": "whisper_load",
                            "model": self._model_name,
                            "device": self._device,
                            "compute_type": self._compute_type,
                        },
                    )
                    self._model = WhisperModel(
                        self._model_name,
                        device=self._device,
                        compute_type=self._compute_type,
                    )
        return self._model

    def transcribe(self, wav_bytes: bytes, language: str = "en") -> str:
        """Transcribe WAV bytes and return the joined text.

        Blocking: callers offload this to a thread so the event loop stays
        responsive. Iterating the segment generator is what runs inference.
        """

        model = self._ensure_model()
        segments, _info = model.transcribe(io.BytesIO(wav_bytes), language=language)
        return " ".join(segment.text.strip() for segment in segments).strip()
