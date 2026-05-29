"""FastAPI wrapper around a TTS :class:`Engine`.

The container starts via:

    uvicorn main:create_app --factory --host 0.0.0.0 --port 8000

so the lifespan can load the engine once at startup, fail the process on a
missing reference voice or model load error, and serve ``/generate`` /
``/health`` / ``/reload`` against the loaded instance.
"""

from __future__ import annotations

import asyncio
import importlib.metadata as _metadata
import io
import logging
import os
import tempfile
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from config import Config
from engine import Engine, GPUOutOfMemoryError, XTTSEngine

logger = logging.getLogger("tts.main")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

# Wrapper's own version, surfaced in /health so the main app's
# /health/ready can aggregate it into components.tts_wrapper.version.
__version__ = "0.1.0"


def _pkg_version(name: str) -> str | None:
    """Installed distribution version, or None if the package isn't present
    (e.g. the test environment runs without torch / coqui-tts)."""

    try:
        return _metadata.version(name)
    except _metadata.PackageNotFoundError:
        return None


# Resolved once at import: installed versions never change for a running
# process, and /health is polled every 30s by the docker healthcheck plus the
# backend's readiness probe -- no point re-scanning dist metadata per request.
_TORCH_VERSION = _pkg_version("torch")
_COQUI_TTS_VERSION = _pkg_version("coqui-tts")

# Build-plan line 822: per-request inference budget. Without this a wedged
# torch call holds the lock forever and the wrapper stops serving anything.
_REQUEST_INFERENCE_TIMEOUT_SECONDS = float(os.environ.get("TTS_REQUEST_TIMEOUT_SECONDS", "120"))


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Cap matches the backend chunker's TTS_CHUNK_MAX_CHARS ceiling plus a
    # small headroom. An oversized payload would hold the asyncio.Lock for
    # the inference window and starve every other /generate call.
    text: str = Field(min_length=1, max_length=4000)
    # Path-safety: episode_id ends up in a filename; reject anything that
    # could escape data_dir/media. Backend uses MD5[:12] hex by default but
    # accept a slightly broader alphabet for hand-curated cases.
    episode_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    chunk_index: int = Field(ge=0, le=100000)


class GenerateResponse(BaseModel):
    wav_path: str
    duration_secs: float
    sample_rate: int


class HealthResponse(BaseModel):
    ok: bool
    model_loaded: bool
    reference_loaded: bool
    version: str | None = None
    torch: str | None = None
    coqui_tts: str | None = None
    device: str | None = None
    sample_rate: int | None = None


def _default_engine_factory() -> Engine:
    return XTTSEngine(Config.from_env())


def create_app(
    *,
    engine: Engine | None = None,
    data_dir: Path | None = None,
) -> FastAPI:
    """Build a FastAPI instance backed by ``engine``.

    Tests pass a fake :class:`Engine` so they exercise the HTTP contract
    without importing Coqui TTS. Production calls this with no args and gets
    the :class:`XTTSEngine` via :func:`_default_engine_factory`.
    """

    chosen_engine = engine if engine is not None else _default_engine_factory()
    chosen_data_dir = data_dir or Path(os.environ.get("DATA_DIR", "/data"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            chosen_engine.load()
        except Exception:
            logger.exception(
                "TTS engine load failed",
                extra={"event": "tts_load_failed"},
            )
            # Re-raise so Starlette's lifespan protocol fires the documented
            # ``lifespan.startup.failed`` path; uvicorn then exits non-zero
            # via the supported Exception contract (not via SystemExit, which
            # bypasses it and can leak into the asyncio cancellation paths).
            raise
        yield
        logger.info("TTS wrapper shutting down")

    app = FastAPI(
        lifespan=lifespan,
        title="audicle-tts-wrapper",
        docs_url="/docs",
    )
    app.state.engine = chosen_engine
    # asyncio.Lock can be constructed outside an event loop; it binds on first
    # acquire. Setting it here means ordering between app.state.engine and
    # app.state.lock is no longer split between module init and lifespan.
    app.state.lock = asyncio.Lock()
    app.state.data_dir = chosen_data_dir

    def get_engine(request: Request) -> Engine:
        return request.app.state.engine

    def get_lock(request: Request) -> asyncio.Lock:
        return request.app.state.lock

    @app.get("/health")
    async def health(engine: Engine = Depends(get_engine)) -> JSONResponse:
        model_loaded = bool(engine.model_loaded)
        reference_loaded = bool(engine.reference_loaded)
        ok = model_loaded and reference_loaded
        body: dict[str, Any] = {
            "ok": ok,
            "model_loaded": model_loaded,
            "reference_loaded": reference_loaded,
            "version": __version__,
            "torch": _TORCH_VERSION,
            "coqui_tts": _COQUI_TTS_VERSION,
            "device": engine.device,
            "sample_rate": engine.sample_rate,
        }
        if not ok:
            # Build-plan line 803 spec: 503 body includes a diagnostic "error".
            reasons = []
            if not model_loaded:
                reasons.append("model not loaded")
            if not reference_loaded:
                reasons.append("reference voice not loaded")
            body["error"] = "; ".join(reasons)
            return JSONResponse(status_code=503, content=body)
        return JSONResponse(status_code=200, content=body)

    @app.post("/generate", response_model=GenerateResponse)
    async def generate(
        body: GenerateRequest,
        engine: Engine = Depends(get_engine),
        lock: asyncio.Lock = Depends(get_lock),
    ) -> GenerateResponse:
        # ``asyncio.Lock`` serializes the GPU inference; concurrent /generate
        # requests queue cleanly at the inference boundary while /health
        # stays responsive (the lock isn't taken on the health path AND
        # engine.synthesize offloads the blocking torch call via
        # asyncio.to_thread, so the event loop can service /health probes
        # while a chunk is in flight).
        async with lock:
            try:
                wav_bytes = await asyncio.wait_for(
                    engine.synthesize(body.text),
                    timeout=_REQUEST_INFERENCE_TIMEOUT_SECONDS,
                )
            except TimeoutError as exc:
                logger.warning(
                    "Inference exceeded per-request timeout",
                    extra={
                        "event": "tts_inference_timeout",
                        "episode_id": body.episode_id,
                        "chunk_index": body.chunk_index,
                        "timeout_secs": _REQUEST_INFERENCE_TIMEOUT_SECONDS,
                    },
                )
                raise HTTPException(
                    status_code=504,
                    detail={
                        "error": "inference timeout",
                        "timeout_secs": _REQUEST_INFERENCE_TIMEOUT_SECONDS,
                    },
                ) from exc
            except GPUOutOfMemoryError as exc:
                logger.warning(
                    "GPU OOM during synthesis; cache cleared",
                    extra={
                        "event": "tts_oom",
                        "episode_id": body.episode_id,
                        "chunk_index": body.chunk_index,
                    },
                )
                raise HTTPException(
                    status_code=500, detail={"error": "GPU OOM", "cause": str(exc)}
                ) from exc

        out_dir = chosen_data_dir / "media"
        out_dir.mkdir(parents=True, exist_ok=True)
        # episode_id is already constrained by the Pydantic pattern; the
        # realpath + commonpath containment check is belt-and-braces against a
        # future regex regression and is the form static analysis recognizes as
        # a path-injection sanitizer (the validated realpath is what gets
        # written, not the raw user input).
        filename = f"{body.episode_id}_chunk_{body.chunk_index}.wav"
        out_real = os.path.realpath(out_dir)
        wav_real = os.path.realpath(os.path.join(out_real, filename))
        if os.path.commonpath([out_real, wav_real]) != out_real:
            raise HTTPException(status_code=400, detail="resolved wav_path escapes data dir")
        wav_path = Path(wav_real)
        _atomic_write_bytes(wav_path, wav_bytes)
        duration = _wav_duration_seconds(wav_bytes)

        logger.info(
            "Chunk synthesized",
            extra={
                "event": "tts_chunk_done",
                "episode_id": body.episode_id,
                "chunk_index": body.chunk_index,
                "duration_secs": duration,
                "wav_path": str(wav_path),
            },
        )
        return GenerateResponse(
            wav_path=str(wav_path),
            duration_secs=duration,
            sample_rate=engine.sample_rate,
        )

    @app.post("/reload")
    async def reload(
        engine: Engine = Depends(get_engine),
        lock: asyncio.Lock = Depends(get_lock),
    ) -> dict[str, Any]:
        async with lock:
            try:
                await engine.reload_reference()
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except Exception as exc:
                logger.exception("reload failed", extra={"event": "tts_reload_failed"})
                raise HTTPException(status_code=500, detail=f"reload failed: {exc}") from exc
        # Build-plan line 805 spec: { ok: true }.
        return {"ok": True}

    return app


def _wav_duration_seconds(wav_bytes: bytes) -> float:
    """Read WAV bytes header to compute duration without re-decoding samples."""

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        return frames / float(rate) if rate else 0.0


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via tempfile + os.replace so consumers can
    never observe a partial WAV.

    Phase 5+ stitching reads each chunk file as soon as it appears; a
    non-atomic ``path.write_bytes`` exposes a racing reader to a truncated
    file. The backend already ships ``services/atomic_write.py``; we keep
    the wrapper's helper inline so the container has no cross-package import.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
