# syntax=docker/dockerfile:1.7

# CUDA + cuDNN runtime image with PyTorch preinstalled.
# Operators on CPU-only hosts should use ``Dockerfile.cpu`` (which builds on a
# plain Python base, since there is no ``pytorch/pytorch:*-cpu`` tag) and set
# ``TTS_DEVICE=cpu``.
FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime AS runtime

# Cache locations target writable paths so the image runs as a non-root user
# (the compose ``user: 1000:1000``). The model caches live on the persistent
# ``/data`` volume (multi-GB HF weights survive container recreation); the rest
# go to image-local ``/tmp``. The old ``/root/.cache`` defaults are mode-700
# root-owned and crash uid 1000 -- Numba "no locator available" and a
# "Permission denied: '/root/.cache'" both trace back to that.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/data/hf_cache \
    TTS_HOME=/data/tts_home \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    NUMBA_CACHE_DIR=/tmp/numba_cache \
    MPLCONFIGDIR=/tmp/mpl

# OS deps: curl for healthchecks; libsndfile for soundfile WAV IO; git so the
# chatterbox backend can install resemble-perth (a git+https dependency).
# ``apt-get upgrade`` pulls every available base-image security patch in one shot
# (openssl/libssl3 CVE-2026-45447, the linux-libc-dev kernel-header CVEs, and the
# gpgv CVE-2025-68973 the old --only-upgrade line targeted by hand). Same-release
# Jammy point updates are ABI-stable; the torch/CUDA stack is pip-installed, not apt.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends curl ca-certificates libsndfile1 git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps from pyproject.toml so this image stays in lockstep with
# the declared dependency set; no parallel hand-written list to drift. The
# trailing -U in install_engine.sh upgrades patch transitive deps the base image
# / resolver pin to vulnerable versions (urllib3, cryptography, pillow, Brotli,
# setuptools, wheel). torch is the base image's 2.6.0, which clears
# CVE-2025-32434 (torch.load RCE), reused as-is.
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
