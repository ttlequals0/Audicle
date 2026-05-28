from __future__ import annotations

import io
import logging
from pathlib import Path

import httpx
import pytest
from app.config import get_settings
from app.services import artwork
from PIL import Image


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    # SSRF guard would NXDOMAIN on example.test; tests exercise the rest of
    # the pipeline via MockTransport, so bypass it for these fixtures.
    async def _allow_all(_host: str) -> None:
        return None

    monkeypatch.setattr(artwork, "_assert_public_host", _allow_all)


def _png_bytes(
    width: int,
    height: int,
    *,
    color: tuple[int, int, int] = (255, 0, 0),
    mode: str = "RGB",
) -> bytes:
    img = Image.new(mode, (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_with_exif(width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), (0, 200, 100))
    buf = io.BytesIO()
    # Inject EXIF (any non-empty bytes object Pillow accepts as ``exif``).
    exif = b"Exif\x00\x00" + b"\x00" * 32
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


# --- happy path -----------------------------------------------------------


async def test_process_artwork_resizes_and_strips_exif(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _jpeg_with_exif(800, 800)
    _patch_async_client(
        monkeypatch,
        httpx.MockTransport(lambda _r: httpx.Response(200, content=src)),
    )

    result = await artwork.process_artwork(
        metadata={"ogImage": "https://example.test/cover.jpg"},
        episode_id="abc123",
        output_dir=tmp_path,
        settings=get_settings(),
    )
    assert result is not None
    assert result.jpg_path.exists()

    # Round-trip the output, confirm it's a 3000x3000 RGB JPEG with no EXIF.
    out = Image.open(result.jpg_path)
    out.load()
    assert out.format == "JPEG"
    assert out.size == (get_settings().ARTWORK_SIZE_PX, get_settings().ARTWORK_SIZE_PX)
    exif = out.getexif()
    assert len(exif) == 0


async def test_process_artwork_center_crops_to_square(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 1200x800 source should center-crop to 800x800 then upscale to target."""

    src = _png_bytes(1200, 800)
    _patch_async_client(
        monkeypatch,
        httpx.MockTransport(lambda _r: httpx.Response(200, content=src)),
    )

    result = await artwork.process_artwork(
        metadata={"ogImage": "https://example.test/landscape.png"},
        episode_id="ep-crop",
        output_dir=tmp_path,
        settings=get_settings(),
    )
    assert result is not None
    out = Image.open(result.jpg_path)
    out.load()
    assert out.size[0] == out.size[1]


# --- fallback paths -------------------------------------------------------


async def test_process_artwork_fallback_when_og_image_missing(
    env: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.services.artwork"):
        result = await artwork.process_artwork(
            metadata={"title": "no image here"},
            episode_id="ep-no-og",
            output_dir=tmp_path,
            settings=get_settings(),
        )
    assert result is None
    assert any(getattr(rec, "reason", "") == "missing_og_image" for rec in caplog.records)


async def test_process_artwork_fallback_on_http_404(
    env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _patch_async_client(
        monkeypatch,
        httpx.MockTransport(lambda _r: httpx.Response(404, text="missing")),
    )
    with caplog.at_level(logging.WARNING, logger="app.services.artwork"):
        result = await artwork.process_artwork(
            metadata={"ogImage": "https://example.test/gone.jpg"},
            episode_id="ep-404",
            output_dir=tmp_path,
            settings=get_settings(),
        )
    assert result is None
    assert any(getattr(rec, "reason", "") == "download_http_error" for rec in caplog.records)


async def test_process_artwork_fallback_on_timeout(
    env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _raise(_request):
        raise httpx.ReadTimeout("slow")

    _patch_async_client(monkeypatch, httpx.MockTransport(_raise))
    with caplog.at_level(logging.WARNING, logger="app.services.artwork"):
        result = await artwork.process_artwork(
            metadata={"ogImage": "https://example.test/slow.jpg"},
            episode_id="ep-slow",
            output_dir=tmp_path,
            settings=get_settings(),
        )
    assert result is None
    assert any(getattr(rec, "reason", "") == "download_unreachable" for rec in caplog.records)


async def test_process_artwork_fallback_on_corrupted_bytes(
    env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _patch_async_client(
        monkeypatch,
        httpx.MockTransport(lambda _r: httpx.Response(200, content=b"not an image")),
    )
    with caplog.at_level(logging.WARNING, logger="app.services.artwork"):
        result = await artwork.process_artwork(
            metadata={"ogImage": "https://example.test/junk.jpg"},
            episode_id="ep-corrupted",
            output_dir=tmp_path,
            settings=get_settings(),
        )
    assert result is None
    assert any(getattr(rec, "reason", "") == "unidentified_format" for rec in caplog.records)


async def test_process_artwork_fallback_when_below_min_source(
    env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 400x400 < ARTWORK_MIN_SOURCE_PX (600 default)
    _patch_async_client(
        monkeypatch,
        httpx.MockTransport(lambda _r: httpx.Response(200, content=_png_bytes(400, 400))),
    )
    with caplog.at_level(logging.WARNING, logger="app.services.artwork"):
        result = await artwork.process_artwork(
            metadata={"ogImage": "https://example.test/small.png"},
            episode_id="ep-small",
            output_dir=tmp_path,
            settings=get_settings(),
        )
    assert result is None
    assert any(getattr(rec, "reason", "") == "source_too_small" for rec in caplog.records)


async def test_process_artwork_fallback_on_svg(
    env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pillow doesn't read SVG; the response body is text/SVG, decode fails,
    fallback log is emitted."""

    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="800" height="800"></svg>'
    _patch_async_client(
        monkeypatch,
        httpx.MockTransport(lambda _r: httpx.Response(200, content=svg)),
    )
    with caplog.at_level(logging.WARNING, logger="app.services.artwork"):
        result = await artwork.process_artwork(
            metadata={"ogImage": "https://example.test/cover.svg"},
            episode_id="ep-svg",
            output_dir=tmp_path,
            settings=get_settings(),
        )
    assert result is None
    assert any(getattr(rec, "reason", "") == "unidentified_format" for rec in caplog.records)


async def test_process_artwork_accepts_rgba_with_alpha(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RGBA inputs (PNGs with transparency) must flatten cleanly to RGB JPG
    instead of crashing on JPEG's lack of alpha support."""

    src = _png_bytes(800, 800, color=(0, 0, 0), mode="RGBA")
    _patch_async_client(
        monkeypatch,
        httpx.MockTransport(lambda _r: httpx.Response(200, content=src)),
    )
    result = await artwork.process_artwork(
        metadata={"ogImage": "https://example.test/alpha.png"},
        episode_id="ep-alpha",
        output_dir=tmp_path,
        settings=get_settings(),
    )
    assert result is not None
    out = Image.open(result.jpg_path)
    out.load()
    assert out.mode == "RGB"


async def test_process_artwork_accepts_og_image_as_list(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Firecrawl can return ogImage as a list when the page declares
    multiple og:image tags; pick the first non-empty string."""

    src = _png_bytes(800, 800)
    _patch_async_client(
        monkeypatch,
        httpx.MockTransport(lambda _r: httpx.Response(200, content=src)),
    )
    result = await artwork.process_artwork(
        metadata={
            "ogImage": [
                "https://example.test/first.png",
                "https://example.test/second.png",
            ]
        },
        episode_id="ep-list",
        output_dir=tmp_path,
        settings=get_settings(),
    )
    assert result is not None
    assert result.source_url == "https://example.test/first.png"


async def test_process_artwork_fallback_on_unsupported_protocol(
    env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A scheme httpx cannot speak (data:, javascript:, file:) raises
    UnsupportedProtocol; it must NOT escape the 'never raises' contract."""

    def _raise(_request):
        raise httpx.UnsupportedProtocol("data scheme unsupported")

    _patch_async_client(monkeypatch, httpx.MockTransport(_raise))
    with caplog.at_level(logging.WARNING, logger="app.services.artwork"):
        result = await artwork.process_artwork(
            metadata={"ogImage": "data:image/png;base64,..."},
            episode_id="ep-data",
            output_dir=tmp_path,
            settings=get_settings(),
        )
    assert result is None
    # Scheme allowlist rejects data: BEFORE httpx is invoked; this verifies
    # the early-out, not the broad httpx.HTTPError catch.
    assert any(getattr(rec, "reason", "") == "blocked_scheme" for rec in caplog.records)


async def test_process_artwork_blocks_non_http_scheme(
    env: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """javascript:, file:, ftp: schemes are rejected by the scheme allowlist
    BEFORE httpx is ever invoked."""

    for url in ("javascript:alert(1)", "file:///etc/passwd", "ftp://example.test/x"):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="app.services.artwork"):
            result = await artwork.process_artwork(
                metadata={"ogImage": url},
                episode_id="ep-bad-scheme",
                output_dir=tmp_path,
                settings=get_settings(),
            )
        assert result is None
        assert any(getattr(rec, "reason", "") == "blocked_scheme" for rec in caplog.records), (
            f"expected blocked_scheme fallback for {url!r}"
        )


async def test_process_artwork_fallback_on_protocol_error_from_httpx(
    env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-timeout, non-network httpx.HTTPError (e.g. RemoteProtocolError
    mid-response) must NOT escape the 'never raises' contract."""

    def _raise(_request):
        raise httpx.RemoteProtocolError("server hung up")

    _patch_async_client(monkeypatch, httpx.MockTransport(_raise))
    with caplog.at_level(logging.WARNING, logger="app.services.artwork"):
        result = await artwork.process_artwork(
            metadata={"ogImage": "https://example.test/broken.jpg"},
            episode_id="ep-proto",
            output_dir=tmp_path,
            settings=get_settings(),
        )
    assert result is None
    assert any(getattr(rec, "reason", "") == "download_unreachable" for rec in caplog.records)


async def test_process_artwork_blocks_private_ip_host(
    env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SSRF guard: hostname resolving to a private IP is rejected. Patches
    artwork._assert_public_host's resolver via the module-level function
    so the test doesn't depend on the host's DNS."""

    async def _block(host: str) -> None:
        raise artwork._BlockedHostError(host, "non_public_address_127.0.0.1")

    monkeypatch.setattr(artwork, "_assert_public_host", _block)
    with caplog.at_level(logging.WARNING, logger="app.services.artwork"):
        result = await artwork.process_artwork(
            metadata={"ogImage": "https://internal-host.example/secret.png"},
            episode_id="ep-ssrf",
            output_dir=tmp_path,
            settings=get_settings(),
        )
    assert result is None
    assert any(getattr(rec, "reason", "") == "blocked_host" for rec in caplog.records)


async def test_process_artwork_fallback_on_oversize_body(
    env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The download must abort once the streamed body exceeds
    ARTWORK_MAX_DOWNLOAD_BYTES, not buffer the whole body to OOM."""

    monkeypatch.setenv("ARTWORK_MAX_DOWNLOAD_BYTES", "1024")
    get_settings.cache_clear()

    huge = b"X" * 8192
    _patch_async_client(
        monkeypatch,
        httpx.MockTransport(lambda _r: httpx.Response(200, content=huge)),
    )
    with caplog.at_level(logging.WARNING, logger="app.services.artwork"):
        result = await artwork.process_artwork(
            metadata={"ogImage": "https://example.test/huge.bin"},
            episode_id="ep-huge",
            output_dir=tmp_path,
            settings=get_settings(),
        )
    assert result is None
    assert any(getattr(rec, "reason", "") == "download_too_large" for rec in caplog.records)


async def test_process_artwork_fallback_on_oversize_content_length(
    env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Advertised Content-Length above the cap aborts before streaming the
    body."""

    monkeypatch.setenv("ARTWORK_MAX_DOWNLOAD_BYTES", "1024")
    get_settings.cache_clear()

    _patch_async_client(
        monkeypatch,
        httpx.MockTransport(
            lambda _r: httpx.Response(
                200,
                content=b"x" * 16,
                headers={"Content-Length": "100000000"},
            )
        ),
    )
    with caplog.at_level(logging.WARNING, logger="app.services.artwork"):
        result = await artwork.process_artwork(
            metadata={"ogImage": "https://example.test/advertised-huge"},
            episode_id="ep-cl-huge",
            output_dir=tmp_path,
            settings=get_settings(),
        )
    assert result is None
    assert any(getattr(rec, "reason", "") == "download_too_large" for rec in caplog.records)


async def test_process_artwork_applies_exif_orientation(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A portrait phone JPEG (Orientation=6) is stored 90deg CW from the
    intended view. The pipeline must transpose BEFORE strip-EXIF or the
    rendered JPG will be sideways forever (no orientation tag left)."""

    img = Image.new("RGB", (1000, 800), (200, 0, 0))
    img.paste(Image.new("RGB", (1000, 80), (0, 200, 0)), (0, 0))
    exif = img.getexif()
    exif[0x0112] = 6  # Orientation tag, 6 = rotate 90 CW
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)

    _patch_async_client(
        monkeypatch,
        httpx.MockTransport(lambda _r: httpx.Response(200, content=buf.getvalue())),
    )
    result = await artwork.process_artwork(
        metadata={"ogImage": "https://example.test/rotated.jpg"},
        episode_id="ep-rot",
        output_dir=tmp_path,
        settings=get_settings(),
    )
    assert result is not None
    rendered = Image.open(result.jpg_path)
    rendered.load()
    # EXIF orientation must be absent or normalized on the output.
    assert rendered.getexif().get(0x0112) in (None, 0, 1)
    assert rendered.size == (
        get_settings().ARTWORK_SIZE_PX,
        get_settings().ARTWORK_SIZE_PX,
    )


async def test_process_artwork_fallback_on_streaming_oversize_chunked(
    env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When Content-Length is absent (chunked transfer), the body-size cap
    must still fire from the streaming loop. The Content-Length pre-check
    test alone does NOT cover this path because httpx auto-populates
    Content-Length from byte content."""

    monkeypatch.setenv("ARTWORK_MAX_DOWNLOAD_BYTES", "1024")
    get_settings.cache_clear()

    class _BigStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(8):
                yield b"X" * 4096

        async def aclose(self) -> None:
            return None

    def _handler(_request):
        # No Content-Length -> server.responses.json:Content-Length is not
        # auto-populated when stream is used.
        return httpx.Response(200, stream=_BigStream())

    _patch_async_client(monkeypatch, httpx.MockTransport(_handler))
    with caplog.at_level(logging.WARNING, logger="app.services.artwork"):
        result = await artwork.process_artwork(
            metadata={"ogImage": "https://example.test/chunked-huge"},
            episode_id="ep-chunked",
            output_dir=tmp_path,
            settings=get_settings(),
        )
    assert result is None
    assert any(getattr(rec, "reason", "") == "download_too_large" for rec in caplog.records)


async def test_process_artwork_fallback_on_decompression_bomb(
    env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pillow raises DecompressionBombError past 2x MAX_IMAGE_PIXELS. Simulate
    by patching Image.open to raise, since constructing a real bomb is
    impractical at unit-test scale."""

    src = _png_bytes(800, 800)
    _patch_async_client(
        monkeypatch,
        httpx.MockTransport(lambda _r: httpx.Response(200, content=src)),
    )
    from PIL import Image as _PILImage

    def _boom(*_args, **_kwargs):
        raise _PILImage.DecompressionBombError("pixel count exceeds limit")

    monkeypatch.setattr(_PILImage, "open", _boom)
    with caplog.at_level(logging.WARNING, logger="app.services.artwork"):
        result = await artwork.process_artwork(
            metadata={"ogImage": "https://example.test/bomb.png"},
            episode_id="ep-bomb",
            output_dir=tmp_path,
            settings=get_settings(),
        )
    assert result is None
    assert any(getattr(rec, "reason", "") == "decompression_bomb" for rec in caplog.records)


async def test_process_artwork_exif_rotation_actually_rotates_pixels(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stronger than the EXIF-tag check: build a source with a colored stripe
    at the top, mark Orientation=6 (rotate 90 CW for display), and confirm
    the stripe lands at the RIGHT edge of the rendered output (which is
    what Orientation=6 means for the viewer)."""

    img = Image.new("RGB", (1000, 800), (0, 0, 0))
    stripe = Image.new("RGB", (1000, 100), (0, 255, 0))
    img.paste(stripe, (0, 0))
    exif = img.getexif()
    exif[0x0112] = 6  # rotate 90 CW for display
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif, quality=95)

    _patch_async_client(
        monkeypatch,
        httpx.MockTransport(lambda _r: httpx.Response(200, content=buf.getvalue())),
    )
    result = await artwork.process_artwork(
        metadata={"ogImage": "https://example.test/portrait.jpg"},
        episode_id="ep-rot-pixels",
        output_dir=tmp_path,
        settings=get_settings(),
    )
    assert result is not None
    rendered = Image.open(result.jpg_path)
    rendered.load()
    target = get_settings().ARTWORK_SIZE_PX
    # After exif_transpose for Orientation=6, the top stripe moves to the
    # RIGHT edge. Sample a column ~10% from the right; if the green stripe
    # really got rotated, pixels here should be predominantly green.
    sample_x = int(target * 0.95)
    sample_y = int(target * 0.5)
    r, g, b = rendered.getpixel((sample_x, sample_y))[:3]
    assert g > 100, (
        f"expected green stripe at right edge after EXIF rotation, got RGB=({r},{g},{b})"
    )


async def test_process_artwork_writes_atomically_no_partial_file(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The atomic write helper renames a temp file in place; after a
    successful save no temp files should remain alongside the output."""

    src = _png_bytes(800, 800)
    _patch_async_client(
        monkeypatch,
        httpx.MockTransport(lambda _r: httpx.Response(200, content=src)),
    )
    result = await artwork.process_artwork(
        metadata={"ogImage": "https://example.test/atomic.png"},
        episode_id="ep-atomic",
        output_dir=tmp_path,
        settings=get_settings(),
    )
    assert result is not None
    assert result.jpg_path.exists()
    remnants = [p for p in tmp_path.iterdir() if p.name.startswith(".artwork-")]
    assert remnants == []
