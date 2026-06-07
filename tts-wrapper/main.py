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
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from config import Config
from engine import Engine, GPUOutOfMemoryError, InferenceBusyError, XTTSEngine
from log_setup import setup_logging
from whisper_verify import WhisperVerifier

setup_logging()
logger = logging.getLogger("tts.main")

# Wrapper's own version, surfaced in /health so the main app's
# /health/ready can aggregate it into components.tts_wrapper.version. The repo
# root VERSION file is the single source. In dev/tests we walk up to it; in the
# image the wrapper build context is tts-wrapper/ (can't see the root file), so
# the build passes it as AUDICLE_WRAPPER_VERSION (from `cat VERSION`).
def _wrapper_version() -> str:
    env = os.environ.get("AUDICLE_WRAPPER_VERSION")
    if env:
        return env.strip()
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "VERSION"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    return "0.0.0"


__version__ = _wrapper_version()


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

# Per-request inference budget. Without this a wedged
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
    # Optional IPA overrides (surface term -> IPA). Honored only by phoneme-capable
    # engines (StyleTTS2); the XTTS engine ignores it. Backward-compatible: the
    # backend only sends it when the live engine reports it supports phonemes.
    pronunciations: dict[str, str] | None = None
    # Optional per-request seed override (Chatterbox only). The backend sends it on
    # a quality regeneration so the re-gen uses a different seed than the bad take;
    # omitted on the first attempt so the wrapper's configured seed applies.
    seed: int | None = None
    # When true and the wrapper has Whisper enabled, transcribe the produced
    # audio with faster-whisper and return it as `transcript`. Backward-compatible:
    # the backend only sends it when WHISPER_VERIFY_ENABLED is on.
    verify: bool = False


class GenerateResponse(BaseModel):
    wav_path: str
    duration_secs: float
    sample_rate: int
    # faster-whisper transcript when verification ran; None otherwise.
    transcript: str | None = None


class HealthResponse(BaseModel):
    ok: bool
    model_loaded: bool
    reference_loaded: bool
    version: str | None = None
    torch: str | None = None
    coqui_tts: str | None = None
    device: str | None = None
    sample_rate: int | None = None
    engine: str | None = None
    supports_phonemes: bool | None = None
    whisper_enabled: bool | None = None
    whisper_model: str | None = None
    whisper_loaded: bool | None = None


def _default_engine_factory() -> Engine:
    cfg = Config.from_env()
    if cfg.engine == "styletts2":
        from style_engine import StyleTTS2Engine  # lazy: heavy deps

        return StyleTTS2Engine(cfg)
    if cfg.engine == "chatterbox":
        from chatterbox_engine import ChatterboxEngine  # lazy: heavy deps

        return ChatterboxEngine(cfg)
    return XTTSEngine(cfg)


def create_app(
    *,
    engine: Engine | None = None,
    data_dir: Path | None = None,
    verifier: WhisperVerifier | None = None,
) -> FastAPI:
    """Build a FastAPI instance backed by ``engine``.

    Tests pass a fake :class:`Engine` so they exercise the HTTP contract
    without importing Coqui TTS. Production calls this with no args and gets
    the :class:`XTTSEngine` via :func:`_default_engine_factory`.

    ``verifier`` is the optional faster-whisper transcriber; when omitted it is
    built from the environment only if ``WHISPER_ENABLED`` is set, so the
    default path (and the tests) never import faster-whisper.
    """

    cfg = Config.from_env()
    chosen_engine = engine if engine is not None else _default_engine_factory()
    chosen_data_dir = data_dir or Path(os.environ.get("DATA_DIR", "/data"))
    chosen_verifier = verifier
    if chosen_verifier is None and cfg.whisper_enabled:
        chosen_verifier = WhisperVerifier(
            cfg.whisper_model, cfg.whisper_device, cfg.whisper_compute_type
        )

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
        # Warm the ASR model at startup (not inside the first /generate, where a
        # multi-GB download would blow the per-request timeout and leave early
        # chunks unverified). A whisper load failure must not take the wrapper
        # down -- verification is optional, so log and degrade to no transcript.
        if chosen_verifier is not None:
            try:
                await asyncio.to_thread(chosen_verifier.load)
                logger.info("Whisper model loaded", extra={"event": "whisper_ready"})
            except Exception:
                logger.exception("Whisper load failed", extra={"event": "whisper_load_failed"})
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

    @app.get("/health/live")
    async def health_live(engine: Engine = Depends(get_engine)) -> JSONResponse:
        # Liveness for orchestration (docker healthcheck, compose
        # depends_on). The wrapper is "up" once the model is loaded, even with
        # no reference voice -- otherwise the app would never start (its
        # depends_on gates on this) and the operator could never reach the UI
        # to upload a voice. /health (readiness) stays 503 until a voice loads.
        model_loaded = bool(engine.model_loaded)
        return JSONResponse(
            status_code=200 if model_loaded else 503,
            content={"ok": model_loaded, "model_loaded": model_loaded},
        )

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
            # getattr with defaults so a minimal fake/legacy engine still serves.
            "engine": getattr(engine, "name", None),
            "supports_phonemes": getattr(engine, "supports_phonemes", None),
            "whisper_enabled": cfg.whisper_enabled,
            "whisper_model": chosen_verifier.model_name if chosen_verifier else None,
            "whisper_loaded": bool(chosen_verifier.loaded) if chosen_verifier else False,
        }
        if not ok:
            # 503 body includes a diagnostic "error".
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
        if not engine.reference_loaded:
            raise HTTPException(
                status_code=503,
                detail="no reference voice loaded; upload one via the UI first",
            )
        logger.info(
            "Generate request received",
            extra={
                "event": "tts_request_received",
                "episode_id": body.episode_id,
                "chunk_index": body.chunk_index,
                "text_chars": len(body.text),
            },
        )
        # The lock serializes GPU inference; /health never takes it, and
        # synthesize offloads the blocking call, so /health stays responsive.
        async with lock:
            inference_started = time.perf_counter()
            try:
                wav_bytes = await asyncio.wait_for(
                    engine.synthesize(body.text, body.pronunciations, body.seed),
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
            except InferenceBusyError as exc:
                # A prior inference (often an orphaned thread left running after a
                # timeout) is still on the GPU. Reject with 503 rather than start
                # a second concurrent inference; the backend retries with backoff.
                logger.warning(
                    "Inference rejected; another is already running",
                    extra={
                        "event": "tts_inference_busy",
                        "episode_id": body.episode_id,
                        "chunk_index": body.chunk_index,
                    },
                )
                raise HTTPException(
                    status_code=503, detail={"error": "inference busy"}
                ) from exc

            # Measure synthesis latency before the optional ASR step so
            # tts_chunk_done.inference_ms stays pure synth time.
            inference_ms = int((time.perf_counter() - inference_started) * 1000)

            # Optional ASR verification, under the same lock so it never runs
            # GPU work concurrently with another chunk's synthesis. A failure
            # here must never fail the chunk -- the backend just gets no
            # transcript and skips its divergence check for this chunk.
            transcript: str | None = None
            if body.verify and chosen_verifier is not None:
                try:
                    transcript = await asyncio.wait_for(
                        asyncio.to_thread(chosen_verifier.transcribe, wav_bytes, cfg.language),
                        timeout=_REQUEST_INFERENCE_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    logger.warning(
                        "ASR verification failed; returning audio without transcript",
                        extra={
                            "event": "whisper_verify_error",
                            "episode_id": body.episode_id,
                            "chunk_index": body.chunk_index,
                            "error": str(exc),
                        },
                    )
                    transcript = None

        out_dir = chosen_data_dir / "media"
        out_dir.mkdir(parents=True, exist_ok=True)
        # episode_id is already constrained by the Pydantic pattern; the
        # containment check below is belt-and-braces against a future regex
        # regression (the validated realpath is what gets written, not the raw
        # user input). Normalize via os.path.realpath (also follows symlinks),
        # then verify the resolved path starts with the data dir -- the
        # realpath + startswith form is the barrier CodeQL's py/path-injection
        # query recognizes as a sanitizer. The root is terminated with os.sep
        # so a sibling like ".../media-evil" can't satisfy the prefix check
        # against ".../media".
        filename = f"{body.episode_id}_chunk_{body.chunk_index}.wav"
        out_real = os.path.realpath(out_dir)
        wav_real = os.path.realpath(os.path.join(out_real, filename))
        if not wav_real.startswith(out_real + os.sep):
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
                "inference_ms": inference_ms,
                "duration_secs": duration,
                "wav_path": str(wav_path),
            },
        )
        return GenerateResponse(
            wav_path=str(wav_path),
            duration_secs=duration,
            sample_rate=engine.sample_rate,
            transcript=transcript,
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
            except InferenceBusyError as exc:
                # An inference (likely an orphaned post-timeout thread) still
                # holds the GPU; recomputing embeddings now would run concurrent
                # GPU work. 503 so the caller retries once the GPU frees.
                raise HTTPException(
                    status_code=503, detail={"error": "inference busy"}
                ) from exc
            except Exception as exc:
                logger.exception("reload failed", extra={"event": "tts_reload_failed"})
                raise HTTPException(status_code=500, detail=f"reload failed: {exc}") from exc
        # { ok: true }.
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

    The audio stage reads each chunk file as soon as it appears; a
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
