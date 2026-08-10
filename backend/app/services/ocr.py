"""OCR for scanned and image-only uploads (0.51.0).

RapidOCR (PaddleOCR-derived ONNX models on onnxruntime, CPU) reads rasterized
pages; pypdfium2 rasterizes PDF pages with its bundled pdfium, no system deps.
This module is only imported by ``file_extraction`` when a parse actually needs
OCR, and rapidocr itself is imported lazily here, so API-server startup never
pays for it.

Two failure modes are deliberate:

- A page sweep whose mean recognition confidence is under ``OCR_MIN_CONFIDENCE``
  raises :class:`OcrLowConfidenceError` -- narrating recognition noise is worse
  than failing the job with a clear message.
- OCR of a long scan runs for minutes inside one worker thread, so the caller
  passes a ``beat`` callback invoked per page to keep the job watchdog fed.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from typing import Any

from app.config import Settings

logger = logging.getLogger("app.services.ocr")

# Languages the shipped model packs actually cover. The wheel bundles the
# PP-OCRv6 det/rec models (Chinese + English); only English is advertised until
# another language's model pack ships in the image. The Settings dropdown and
# the OCR_LANGUAGE validation both read this tuple, so adding a language later
# is one model pack plus one entry here -- no schema or API change.
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en",)
DEFAULT_LANGUAGE = "en"


class OcrError(Exception):
    """Base for OCR failures the extraction layer converts to job errors."""


class OcrLowConfidenceError(OcrError):
    """Recognition confidence too low to trust the text."""


_engine: Any = None


def _get_engine() -> Any:
    """The shared RapidOCR engine, constructed on first use (models load in
    about a second and stay resident in the worker process)."""

    global _engine
    if _engine is None:
        from rapidocr import RapidOCR

        _engine = RapidOCR()
    return _engine


def _run_page(image: Any) -> tuple[str, list[float]]:
    """OCR one page image: (joined text lines, per-line confidences)."""

    result = _get_engine()(image)
    texts = tuple(result.txts or ())
    scores = list(result.scores or ())
    return "\n".join(texts), scores


def _finish(
    page_texts: list[str], scores: list[float], settings: Settings, pages: int
) -> str:
    text = "\n\n".join(t for t in page_texts if t.strip())
    if not scores or not text.strip():
        raise OcrLowConfidenceError(
            f"OCR found no readable text in {pages} page(s)"
        )
    mean_confidence = sum(scores) / len(scores)
    if mean_confidence < settings.OCR_MIN_CONFIDENCE:
        raise OcrLowConfidenceError(
            f"OCR mean confidence {mean_confidence:.2f} is below the "
            f"{settings.OCR_MIN_CONFIDENCE:.2f} floor; refusing to narrate noise"
        )
    logger.info(
        "OCR complete",
        extra={
            "event": "ocr_complete",
            "pages": pages,
            "chars": len(text),
            "mean_confidence": round(mean_confidence, 3),
        },
    )
    return text


def ocr_pdf(data: bytes, settings: Settings, beat: Callable[[], None]) -> str:
    """Rasterize and OCR up to ``OCR_MAX_PAGES`` pages of a scanned PDF."""

    import numpy as np
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(data)
    try:
        total = len(document)
        page_count = min(total, settings.OCR_MAX_PAGES)
        if page_count < total:
            logger.warning(
                "OCR page cap reached; trailing pages skipped",
                extra={"event": "ocr_pages_capped", "total": total, "kept": page_count},
            )
        scale = settings.OCR_DPI / 72
        page_texts: list[str] = []
        scores: list[float] = []
        for index in range(page_count):
            bitmap = document[index].render(scale=scale)
            image = np.asarray(bitmap.to_pil().convert("RGB"))
            text, page_scores = _run_page(image)
            page_texts.append(text)
            scores.extend(page_scores)
            beat()  # one watchdog beat per page, like the TTS per-attempt beat
    finally:
        document.close()
    return _finish(page_texts, scores, settings, pages=page_count)


def ocr_image(data: bytes, settings: Settings, beat: Callable[[], None]) -> str:
    """OCR a single uploaded image (png/jpg/webp/tiff)."""

    import numpy as np
    from PIL import Image, UnidentifiedImageError

    try:
        image = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise OcrError(f"could not decode image: {exc}") from exc
    text, scores = _run_page(image)
    beat()
    return _finish([text], scores, settings, pages=1)
