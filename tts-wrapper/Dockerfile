# syntax=docker/dockerfile:1.7

# Plain slim base plus the official PyTorch CUDA wheel avoids carrying an
# unrelated conda environment and its operating-system packages.
# Operators on CPU-only hosts should use ``Dockerfile.cpu`` and TTS_DEVICE=cpu.
#
# PYVER single-sources the Python version for the FROM line AND the nvidia
# wheel paths in LD_LIBRARY_PATH below -- bump it in one place.
ARG PYVER=3.11
FROM ghcr.io/astral-sh/uv:0.12.10 AS uv
FROM python:${PYVER}-slim AS runtime
ARG PYVER
COPY --from=uv /uv /uvx /bin/

# The GPU image is amd64-only by construction: on arm64, pip resolves the
# CPU-only aarch64 torch wheel (no nvidia-* deps) and the build would silently
# produce a CUDA-less "GPU" image. The old pytorch base failed at FROM on
# arm64; keep that fail-fast, with a clearer message.
ARG TARGETARCH
RUN [ "$TARGETARCH" = "amd64" ] || { \
      echo "ERROR: the GPU wrapper image is amd64-only (arm64 pip torch has no CUDA); build with --platform linux/amd64 or use Dockerfile.cpu" >&2; \
      exit 1; \
    }

# Cache locations target writable paths so the image runs as a non-root user
# (the compose ``user: 1000:1000``). The model caches live on the persistent
# ``/data`` volume (multi-GB HF weights survive container recreation); the rest
# go to image-local ``/tmp``. The old ``/root/.cache`` defaults are mode-700
# root-owned and crash uid 1000 -- Numba "no locator available" and a
# "Permission denied: '/root/.cache'" both trace back to that.
#
# NVIDIA_VISIBLE_DEVICES / NVIDIA_DRIVER_CAPABILITIES replace what the CUDA
# base image declared. NVIDIA_REQUIRE_CUDA is NEW (the base had none): it
# fails the container start on drivers below the CUDA 12.6 floor instead of
# erroring mid-job. LD_LIBRARY_PATH: ctranslate2 (faster-whisper) dlopens
# cuDNN/cuBLAS from the pip nvidia wheels; torch finds its own libs without
# help. The wheel paths derive from PYVER (single-sourced with FROM); the
# trailing /usr/local/nvidia entries preserve the old base's legacy
# nvidia-docker driver-mount paths.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/data/hf_cache \
    TTS_HOME=/data/tts_home \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    NUMBA_CACHE_DIR=/tmp/numba_cache \
    MPLCONFIGDIR=/tmp/mpl \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    NVIDIA_REQUIRE_CUDA="cuda>=12.6" \
    LD_LIBRARY_PATH=/usr/local/lib/python${PYVER}/site-packages/nvidia/cudnn/lib:/usr/local/lib/python${PYVER}/site-packages/nvidia/cublas/lib:/usr/local/nvidia/lib:/usr/local/nvidia/lib64

# OS deps: curl for healthchecks; libsndfile for soundfile WAV IO; git so the
# chatterbox backend can install resemble-perth (a git+https dependency).
# ``apt-get upgrade`` pulls every available base-image security patch.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends curl ca-certificates libsndfile1 git \
    && rm -rf /var/lib/apt/lists/*

# Install torch first so the CUDA wheel only downloads when its pin changes.
RUN pip install --no-cache-dir torch==2.13.0 torchaudio==2.11.0 \
        --index-url https://download.pytorch.org/whl/cu126

WORKDIR /app

# Install the remaining Python dependencies from the reviewed lock. The
# preinstalled CUDA torch satisfies the version exported from that lock.
COPY pyproject.toml uv.lock LICENSE ./
# Every module main.py imports must be listed here. A missing one is not caught
# by tests (they import from the source tree) and only surfaces as an
# ImportError crash-loop after deploy.
COPY config.py engine.py log_setup.py main.py memory.py chatterbox_engine.py whisper_verify.py install_engine.sh ./
# install_engine.sh is shared with Dockerfile.cpu.
RUN sh install_engine.sh

EXPOSE 8000

# Single-source version: the build passes the repo-root VERSION value here
# (`--build-arg WRAPPER_VERSION=$(cat VERSION)`) since this build context is
# tts-wrapper/ and can't read the root file. main.py reads AUDICLE_WRAPPER_VERSION.
ARG WRAPPER_VERSION=0.0.0
ENV AUDICLE_WRAPPER_VERSION=${WRAPPER_VERSION}

# start-period is generous: the first boot downloads multi-GB model weights from
# HuggingFace before the model loads and /health/live turns 200.
HEALTHCHECK --interval=30s --timeout=10s --retries=5 --start-period=300s \
    CMD curl -fsS http://localhost:8000/health/live || exit 1

# ``--factory`` so create_app() is called once with the configured engine.
# ``--workers 1`` because the model holds GPU resources that don't survive
# forking; the asyncio.Lock inside the app serializes per-process /generate calls.
CMD ["uvicorn", "main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "300"]
