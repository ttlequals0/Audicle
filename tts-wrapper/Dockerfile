# syntax=docker/dockerfile:1.7

# CUDA + cuDNN runtime image with PyTorch preinstalled.
# Operators on CPU-only hosts should use ``Dockerfile.cpu`` (which builds on a
# plain Python base, since there is no ``pytorch/pytorch:*-cpu`` tag) and set
# ``TTS_DEVICE=cpu``.
FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime AS runtime

# Cache locations target writable paths so the image runs as a non-root user
# (the compose ``user: 1000:1000``). The model caches live on the persistent
# ``/data`` volume (multi-GB XTTS/HF weights survive container recreation);
# the rest go to image-local ``/tmp``. The old ``/root/.cache`` defaults are
# mode-700 root-owned and crash uid 1000 -- Numba "no locator available" and
# Coqui "Permission denied: '/root/.cache'" both trace back to that.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/data/hf_cache \
    TTS_HOME=/data/tts_home \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    NUMBA_CACHE_DIR=/tmp/numba_cache \
    MPLCONFIGDIR=/tmp/mpl \
    # Coqui gates the first XTTS-v2 load behind an interactive y/n CPML prompt;
    # in a container there is no TTY to answer it, so the load hangs/fails.
    # Self-hosting the wrapper is acceptance of the non-commercial CPML (see the
    # README license note); agree non-interactively so the model loads headless.
    COQUI_TOS_AGREED=1

# OS deps: curl for healthchecks; libsndfile for soundfile WAV IO; git so the
# chatterbox backend can install resemble-perth (a git+https dependency).
# --only-upgrade gpgv pulls the patched base-image package (CVE-2025-68973).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates libsndfile1 git \
    && apt-get install -y --only-upgrade gpgv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps from pyproject.toml so this image stays in lockstep with
# the declared dependency set; no parallel hand-written list to drift.
# transformers: coqui-tts 0.24 requires >=4.43.0 unbounded but breaks on 5.x, so
# we pin the 4.48.x line -- >=4.48.0 clears CVE-2024-11392/-11393/-11394 while
# staying well under the 5.x break. The trailing -U upgrades patch transitive
# deps the base image / resolver pin to vulnerable versions (urllib3,
# cryptography, pillow, Brotli, setuptools, wheel). torch is the base image's
# 2.6.0, which clears CVE-2025-32434 (torch.load RCE). torch 2.6 defaults
# torch.load to weights_only=True, which would break XTTS checkpoint loading;
# engine.py registers the XTTS config classes via torch.serialization.
# add_safe_globals before load so the trusted checkpoint deserializes.
COPY pyproject.toml ./
COPY config.py engine.py log_setup.py main.py style_engine.py chatterbox_engine.py whisper_verify.py install_engine.sh ./
# TTS_BACKEND selects which engine extra to install (xtts | styletts2 |
# chatterbox); the engine is then chosen at runtime via TTS_ENGINE. install_engine.sh
# (shared with Dockerfile.cpu) installs exactly one engine plus security patches;
# torch is the base image's 2.6.0 (clears CVE-2025-32434), reused as-is.
ARG TTS_BACKEND=chatterbox
RUN sh install_engine.sh "$TTS_BACKEND"

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
