"""Episode artwork pipeline.

Source is the article's ``og:image`` from Firecrawl metadata. The image is
downloaded under a size cap, validated, EXIF-rotated, center-cropped to
square, resized to ``ARTWORK_SIZE_PX``, saved as JPG quality
``ARTWORK_JPG_QUALITY`` with EXIF stripped, and stored atomically at
``/data/media/{episode_id}.jpg``.

Every failure mode falls back to feed-level artwork: the function returns
``None`` and emits a structured ``artwork_fallback`` WARN log so operators
can spot patterns. The RSS feed renders ``FEED_ARTWORK_URL`` for
episodes whose row has ``artwork_path = NULL``.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import Settings
from app.services import ssrf
from app.services.atomic_write import write_bytes_atomic

logger = logging.getLogger("app.services.artwork")


@dataclass(frozen=True)
class ArtworkResult:
    jpg_path: Path
    source_url: str
    # A downsized copy of the same cover, for embedding into the episode MP3 (ID3 APIC)
    # so players that ignore the feed's per-episode art still show it.
    embed_jpg_bytes: bytes


# Pillow formats we treat as supported. Anything else (notably SVG) falls
# through to feed-art via the fallback path.
_SUPPORTED_FORMATS = frozenset({"JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF", "MPO"})


async def process_artwork(
    metadata: dict[str, Any],
    episode_id: str,
    output_dir: Path,
    settings: Settings,
) -> ArtworkResult | None:
    """Download and process the article's ``og:image``.

    Returns the local JPG path on success, ``None`` on any documented failure
    (fall back to feed-level artwork). Never raises -- every error path logs
    the reason and returns ``None``.
    """

    source_url = _extract_og_image(metadata)
    if not source_url:
        _log_fallback("missing_og_image", episode_id, None)
        return None

    scheme_error = _validate_scheme(source_url)
    if scheme_error is not None:
        _log_fallback("blocked_scheme", episode_id, source_url, scheme=scheme_error)
        return None

    try:
        data = await _download(source_url, settings)
    except ssrf.BlockedHostError as exc:
        _log_fallback(
            "blocked_host",
            episode_id,
            source_url,
            host=exc.host,
            block_reason=exc.reason,
        )
        return None
    except httpx.TimeoutException as exc:
        _log_fallback(
            "download_unreachable",
            episode_id,
            source_url,
            error_class=type(exc).__name__,
            error=str(exc),
        )
        return None
    except _HttpError as exc:
        _log_fallback(
            "download_http_error",
            episode_id,
            source_url,
            status_code=exc.status_code,
        )
        return None
    except _DownloadTooLargeError as exc:
        _log_fallback(
            "download_too_large",
            episode_id,
            source_url,
            limit_bytes=exc.limit_bytes,
        )
        return None
    except httpx.HTTPError as exc:
        # Covers NetworkError + InvalidURL + UnsupportedProtocol +
        # TooManyRedirects + DecodingError + RemoteProtocolError, all of
        # which are reachable from attacker-controlled og:image URLs and
        # would otherwise escape the "never raises" contract.
        _log_fallback(
            "download_unreachable",
            episode_id,
            source_url,
            error_class=type(exc).__name__,
            error=str(exc),
        )
        return None

    try:
        rendered = _decode_and_render(data, settings)
    except UnidentifiedImageError:
        _log_fallback("unidentified_format", episode_id, source_url)
        return None
    except _UnsupportedFormatError as exc:
        _log_fallback("unsupported_format", episode_id, source_url, fmt=exc.fmt)
        return None
    except _SourceTooSmallError as exc:
        _log_fallback(
            "source_too_small",
            episode_id,
            source_url,
            width=exc.width,
            height=exc.height,
            min_px=settings.ARTWORK_MIN_SOURCE_PX,
        )
        return None
    except Image.DecompressionBombError as exc:
        _log_fallback(
            "decompression_bomb",
            episode_id,
            source_url,
            error=str(exc),
        )
        return None
    except (OSError, ValueError) as exc:
        _log_fallback(
            "pillow_decode_failed",
            episode_id,
            source_url,
            error_class=type(exc).__name__,
            error=str(exc),
        )
        return None

    output_path = output_dir / f"{episode_id}.jpg"
    try:
        write_bytes_atomic(output_path, rendered.jpg_bytes, prefix=".artwork-")
    except OSError as exc:
        _log_fallback(
            "atomic_write_failed",
            episode_id,
            source_url,
            error_class=type(exc).__name__,
            error=str(exc),
        )
        return None

    logger.info(
        "Episode artwork rendered",
        extra={
            "event": "artwork_done",
            "episode_id": episode_id,
            "source_url": source_url,
            "output": str(output_path),
            "source_width": rendered.source_width,
            "source_height": rendered.source_height,
            "target_px": settings.ARTWORK_SIZE_PX,
            "jpg_bytes": len(rendered.jpg_bytes),
        },
    )
    return ArtworkResult(
        jpg_path=output_path, source_url=source_url, embed_jpg_bytes=rendered.embed_jpg_bytes
    )


def _extract_og_image(metadata: dict[str, Any]) -> str | None:
    """Firecrawl returns ``ogImage`` in the metadata dict; older versions use
    ``og:image``. Accept either, and accept list values (some pages declare
    multiple og:image tags and Firecrawl forwards the array)."""

    if not isinstance(metadata, dict):
        return None
    for key in ("ogImage", "og:image", "og_image"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item.strip()
    return None


class _HttpError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _DownloadTooLargeError(RuntimeError):
    def __init__(self, limit_bytes: int) -> None:
        super().__init__(f"download exceeded {limit_bytes} bytes")
        self.limit_bytes = limit_bytes


class _UnsupportedFormatError(RuntimeError):
    def __init__(self, fmt: str) -> None:
        super().__init__(f"unsupported format {fmt!r}")
        self.fmt = fmt


class _SourceTooSmallError(RuntimeError):
    def __init__(self, width: int, height: int) -> None:
        super().__init__(f"source {width}x{height} below minimum")
        self.width = width
        self.height = height


_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _validate_scheme(url: str) -> str | None:
    """Return the offending scheme if not http/https, else None."""

    scheme = urlsplit(url).scheme.lower()
    if scheme in _ALLOWED_SCHEMES:
        return None
    return scheme or "<empty>"


async def _download(url: str, settings: Settings) -> bytes:
    """Download ``url`` with a hard byte cap.

    Streams the response and aborts once ``ARTWORK_MAX_DOWNLOAD_BYTES`` is
    exceeded so a hostile URL can't OOM the worker by serving an unbounded
    body within the fetch timeout. Content-Length is checked first when
    advertised; the streaming check covers chunked-transfer responses that
    omit it.
    """

    parts = urlsplit(url)
    host = parts.hostname or ""
    pinned_ip = await ssrf.resolve_public_host(host)

    # Closes the DNS-rebinding TOCTOU: a hostile DNS server could otherwise
    # answer the resolver check with a public IP and httpx's subsequent
    # lookup with a private one. We pass the IP literal to httpx (so the TCP
    # connection goes to the validated IP) but set the ``Host`` header AND the
    # ``sni_hostname`` request extension to the original name -- httpx derives
    # the TLS SNI from the URL host, which is now an IP, so without the extension
    # the handshake carries no/wrong SNI and SNI-based hosts (every CDN) reject
    # it with SSLV3_ALERT_HANDSHAKE_FAILURE.
    pinned_url = ssrf.pin_url_to_ip(url, pinned_ip)

    timeout = httpx.Timeout(settings.ARTWORK_FETCH_TIMEOUT_SECONDS)
    max_bytes = settings.ARTWORK_MAX_DOWNLOAD_BYTES
    async with (
        httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            event_hooks={"request": [ssrf.build_redirect_pin_hook(pinned_ip)]},
        ) as client,
        client.stream(
            "GET",
            pinned_url,
            headers={"Host": host},
            extensions={"sni_hostname": host},
        ) as response,
    ):
        if not response.is_success:
            raise _HttpError(response.status_code)
        advertised = response.headers.get("Content-Length")
        if advertised is not None:
            try:
                if int(advertised) > max_bytes:
                    raise _DownloadTooLargeError(max_bytes)
            except ValueError:
                pass  # malformed header; rely on the streaming check
        buffer = bytearray()
        async for chunk in response.aiter_bytes():
            buffer.extend(chunk)
            if len(buffer) > max_bytes:
                raise _DownloadTooLargeError(max_bytes)
        return bytes(buffer)


@dataclass(frozen=True)
class _RenderedImage:
    jpg_bytes: bytes
    embed_jpg_bytes: bytes
    source_width: int
    source_height: int


def _encode_jpeg(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True, exif=b"")
    return buf.getvalue()


def _decode_and_render(data: bytes, settings: Settings) -> _RenderedImage:
    """Decode ``data``, validate, EXIF-rotate, flatten, crop, resize, encode
    JPG. Raises typed exceptions; the caller maps each to a fallback reason.
    """

    with Image.open(io.BytesIO(data)) as opened:
        opened.load()
        fmt = (opened.format or "").upper()
        if fmt not in _SUPPORTED_FORMATS:
            raise _UnsupportedFormatError(fmt)

        # Honor EXIF orientation BEFORE measuring dimensions. A portrait
        # phone JPEG (Orientation=6, 90deg CW) reports its on-disk
        # width/height which are the post-rotation values swapped; comparing
        # them to ARTWORK_MIN_SOURCE_PX without transposing would
        # mis-classify the source, and we'd save the pixels sideways since
        # ``exif=b""`` strips the only hint clients have.
        oriented = ImageOps.exif_transpose(opened)
        width, height = oriented.size
        if width < settings.ARTWORK_MIN_SOURCE_PX or height < settings.ARTWORK_MIN_SOURCE_PX:
            raise _SourceTooSmallError(width, height)

        flat = _flatten_to_rgb(oriented)
        square = _center_crop_square(flat)
        target = settings.ARTWORK_SIZE_PX
        if square.size != (target, target):
            square = square.resize((target, target), Image.Resampling.LANCZOS)

        quality = settings.ARTWORK_JPG_QUALITY
        # A smaller copy of the same square for embedding into the MP3 (ID3 APIC).
        # Clamp to the master size so a misconfigured embed size can't upscale and bloat it.
        embed_px = min(settings.EMBED_ARTWORK_SIZE_PX, target)
        embed_img = (
            square
            if square.size == (embed_px, embed_px)
            else square.resize((embed_px, embed_px), Image.Resampling.LANCZOS)
        )
        return _RenderedImage(
            jpg_bytes=_encode_jpeg(square, quality),
            embed_jpg_bytes=_encode_jpeg(embed_img, quality),
            source_width=width,
            source_height=height,
        )


def _flatten_to_rgb(img: Image.Image) -> Image.Image:
    """Composite onto a black background when the source has an alpha or
    palette-with-alpha mode; JPEG has no alpha channel."""

    mode = img.mode
    if mode in ("RGBA", "LA", "P", "PA"):
        rgba = img if mode == "RGBA" else img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (0, 0, 0))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    if mode != "RGB":
        return img.convert("RGB")
    return img


def _center_crop_square(img: Image.Image) -> Image.Image:
    width, height = img.size
    if width == height:
        return img
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return img.crop((left, top, left + side, top + side))


def _log_fallback(reason: str, episode_id: str, source_url: str | None, **extra: Any) -> None:
    logger.warning(
        "Artwork fallback",
        extra={
            "event": "artwork_fallback",
            "reason": reason,
            "episode_id": episode_id,
            "source_url": source_url,
            **extra,
        },
    )
