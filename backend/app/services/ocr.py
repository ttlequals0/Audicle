"""OCR for scanned and image-only uploads (0.51.0).

RapidOCR (PaddleOCR-derived ONNX models on onnxruntime, CPU) reads rasterized
pages; pypdfium2 rasterizes PDF pages with its bundled pdfium, no system deps.
This module is only imported by ``file_extraction`` when a parse actually needs
OCR, and rapidocr itself is imported lazily here, so API-server startup never
pays for it.

Two deliberate behaviors: a sweep under ``OCR_MIN_CONFIDENCE`` raises
:class:`OcrLowConfidenceError` rather than narrating noise, and the caller's
``beat`` callback fires per page so a long scan keeps the watchdog fed.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from typing import Any

from app.config import Settings

logger = logging.getLogger("app.services.ocr")

# Languages the shipped model packs cover. The wheel bundles PP-OCRv6
# (Chinese + English); only English is advertised until another pack ships.
# The Settings dropdown and OCR_LANGUAGE validation both read this tuple.
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en",)
DEFAULT_LANGUAGE = "en"


class OcrError(Exception):
    """Base for OCR failures the extraction layer converts to job errors."""


class OcrLowConfidenceError(OcrError):
    """Recognition confidence too low to trust the text."""


_engine: Any = None


def _get_engine() -> Any:
    """The shared RapidOCR engine, built on first use and kept resident."""

    global _engine
    if _engine is None:
        from rapidocr import RapidOCR

        _engine = RapidOCR()
    return _engine


def _run_page(image: Any) -> tuple[str, list[float]]:
    """OCR one page image: (joined text lines, per-line confidences)."""

    try:
        result = _get_engine()(image)
    except Exception as exc:
        # Engine-internal failures (ONNX, cv2) become the one error type the
        # extraction layer converts to a clear job failure.
        raise OcrError(f"OCR engine failed: {exc}") from exc
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
    """Rasterize and OCR up to ``OCR_MAX_PAGES`` pages of a scanned PDF.

    Runs blocking, and ``beat`` fires once per page off the event loop.
    """

    import numpy as np
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(data)
    except Exception as exc:
        raise OcrError(f"could not open PDF for OCR: {exc}") from exc
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
            try:
                bitmap = document[index].render(scale=scale)
                image = np.asarray(bitmap.to_pil().convert("RGB"))
            except Exception as exc:
                raise OcrError(f"could not rasterize PDF page {index + 1}: {exc}") from exc
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
