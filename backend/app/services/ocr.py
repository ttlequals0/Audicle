"""OCR for scanned and image-only uploads (0.51.0).

RapidOCR (PaddleOCR-derived ONNX models on onnxruntime, CPU) reads rasterized
pages; pypdfium2 rasterizes PDF pages with its bundled pdfium, no system deps.
The RapidOCR engine is initialized lazily on the first OCR request, so startup
does not load its ONNX models.

Two deliberate behaviors: a sweep under ``OCR_MIN_CONFIDENCE`` raises
:class:`OcrLowConfidenceError` rather than narrating noise, and the caller's
``beat`` callback fires per page so a long scan keeps the watchdog fed.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from typing import Any

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, UnidentifiedImageError
from rapidocr import RapidOCR

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


def _finish(page_texts: list[str], scores: list[float], settings: Settings, pages: int) -> str:
    text = "\n\n".join(t for t in page_texts if t.strip())
    if not scores or not text.strip():
        raise OcrLowConfidenceError(f"OCR found no readable text in {pages} page(s)")
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
    """Rasterize and OCR every page in a scanned PDF within ``OCR_MAX_PAGES``.

    Oversized documents fail instead of producing partial narration. Runs blocking,
    and ``beat`` fires once per page off the event loop.
    """

    pages = ocr_pdf_pages(data, settings, beat, None)
    return "\n\n".join(text for text in pages.values() if text.strip())


def ocr_pdf_pages(
    data: bytes,
    settings: Settings,
    beat: Callable[[], None],
    page_indices: list[int] | None,
) -> dict[int, str]:
    """OCR selected zero-based pages, capped by ``OCR_MAX_PAGES``."""

    try:
        document = pdfium.PdfDocument(data)
    except Exception as exc:
        raise OcrError(f"could not open PDF for OCR: {exc}") from exc
    try:
        total = len(document)
        if page_indices is None:
            if total > settings.OCR_MAX_PAGES:
                raise OcrError(
                    f"PDF has {total} pages, exceeding OCR_MAX_PAGES="
                    f"{settings.OCR_MAX_PAGES}; refusing partial OCR"
                )
            indices = list(range(total))
        else:
            indices = sorted(set(page_indices))
            if any(index < 0 or index >= total for index in indices):
                raise OcrError("selected OCR page is outside the PDF")
            if len(indices) > settings.OCR_MAX_PAGES:
                raise OcrError(
                    f"PDF has {len(indices)} scanned pages, exceeding OCR_MAX_PAGES="
                    f"{settings.OCR_MAX_PAGES}; refusing partial OCR"
                )
        scale = settings.OCR_DPI / 72
        page_texts: dict[int, str] = {}
        scores: list[float] = []
        for index in indices:
            try:
                bitmap = document[index].render(scale=scale)
                image = np.asarray(bitmap.to_pil().convert("RGB"))
            except Exception as exc:
                raise OcrError(f"could not rasterize PDF page {index + 1}: {exc}") from exc
            text, page_scores = _run_page(image)
            if not text.strip() or not page_scores:
                raise OcrLowConfidenceError(f"OCR found no readable text on PDF page {index + 1}")
            confidence = sum(page_scores) / len(page_scores)
            if confidence < settings.OCR_MIN_CONFIDENCE:
                raise OcrLowConfidenceError(
                    f"OCR confidence {confidence:.2f} on PDF page {index + 1} is below the "
                    f"{settings.OCR_MIN_CONFIDENCE:.2f} floor"
                )
            page_texts[index] = text
            scores.extend(page_scores)
            beat()
    finally:
        document.close()
    if page_texts:
        _finish(list(page_texts.values()), scores, settings, pages=len(indices))
    return page_texts


def ocr_image(data: bytes, settings: Settings, beat: Callable[[], None]) -> str:
    """OCR a single uploaded image (png/jpg/webp/tiff)."""

    try:
        image = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise OcrError(f"could not decode image: {exc}") from exc
    text, scores = _run_page(image)
    beat()
    return _finish([text], scores, settings, pages=1)
