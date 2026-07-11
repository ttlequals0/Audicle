# syntax=docker/dockerfile:1.7

# Plain slim base + pip torch. The old pytorch/pytorch:2.6.0 CUDA base (Ubuntu
# 22.04 + conda) added ~180 unfixable OS CVEs while providing nothing the pip
# install does not: torch 2.6.0's PyPI linux wheel is the cu124 build and pulls
# the full nvidia-* wheel set (CUDA runtime, cuDNN 9.1, cuBLAS). Same Python
# (3.11), same torch (2.6.0, chatterbox's hard pin), same driver floor (525+).
# Operators on CPU-only hosts should use ``Dockerfile.cpu`` and TTS_DEVICE=cpu.
FROM python:3.11-slim AS runtime

# Cache locations target writable paths so the image runs as a non-root user
# (the compose ``user: 1000:1000``). The model caches live on the persistent
# ``/data`` volume (multi-GB HF weights survive container recreation); the rest
# go to image-local ``/tmp``. The old ``/root/.cache`` defaults are mode-700
# root-owned and crash uid 1000 -- Numba "no locator available" and a
# "Permission denied: '/root/.cache'" both trace back to that.
#
# NVIDIA_* replaces what the CUDA base image used to declare: the container
# runtime exposes all GPUs with compute capability, and NVIDIA_REQUIRE_CUDA
# fails the container start on drivers older than the cu124 wheels' floor
# (driver 525+) instead of surfacing a torch runtime error mid-job.
# LD_LIBRARY_PATH points at the pip nvidia wheels for ctranslate2
# (faster-whisper): it statically links the CUDA runtime but dlopens
# cuDNN/cuBLAS; torch itself finds its nvidia libs without help. The paths
# embed python3.11 -- update them together with the FROM line's Python.
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
    NVIDIA_REQUIRE_CUDA="cuda>=12.0" \
    LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib:/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib

# OS deps: curl for healthchecks; libsndfile for soundfile WAV IO; git so the
# chatterbox backend can install resemble-perth (a git+https dependency).
# ``apt-get upgrade`` pulls every available base-image security patch.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends curl ca-certificates libsndfile1 git \
    && rm -rf /var/lib/apt/lists/*

# torch first, as its own layer: the ~2.5GB cu124 wheel set only re-downloads
# when the torch pin changes, not on every wrapper source edit. The plain PyPI
# wheel IS the CUDA build on linux/x86_64 (nvidia-* wheels come as deps);
# chatterbox's torch==2.6.0 pin is then already satisfied at engine install.
RUN pip install --no-cache-dir torch==2.6.0

WORKDIR /app

# Install Python deps from pyproject.toml so this image stays in lockstep with
# the declared dependency set; no parallel hand-written list to drift. The
# trailing -U in install_engine.sh upgrades patch transitive deps the resolver
# pins to vulnerable versions (urllib3, cryptography, pillow, Brotli,
# setuptools, wheel). torch 2.6.0 is preinstalled above; chatterbox reuses it.
COPY pyproject.toml ./
COPY config.py engine.py log_setup.py main.py chatterbox_engine.py whisper_verify.py install_engine.sh ./
# install_engine.sh (shared with Dockerfile.cpu) installs the Chatterbox engine
# plus the optional faster-whisper backend and security patches.
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
