# syntax=docker/dockerfile:1.7

# CUDA + cuDNN runtime image with PyTorch preinstalled.
# Operators on CPU-only hosts should use ``Dockerfile.cpu`` (which builds on a
# plain Python base, since there is no ``pytorch/pytorch:*-cpu`` tag) and set
# ``TTS_DEVICE=cpu``.
FROM pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/root/.cache/huggingface \
    TTS_HOME=/root/.cache/huggingface/tts \
    # Coqui gates the first XTTS-v2 load behind an interactive y/n CPML prompt;
    # in a container there is no TTY to answer it, so the load hangs/fails.
    # Self-hosting the wrapper is acceptance of the non-commercial CPML (see the
    # README license note); agree non-interactively so the model loads headless.
    COQUI_TOS_AGREED=1

# OS deps: curl for healthchecks; libsndfile for soundfile/coqui-tts WAV IO.
# --only-upgrade gpgv pulls the patched base-image package (CVE-2025-68973).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates libsndfile1 \
    && apt-get install -y --only-upgrade gpgv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps from pyproject.toml so this image stays in lockstep with
# the declared dependency set; no parallel hand-written list to drift.
# transformers: coqui-tts 0.24 requires >=4.43.0 unbounded but breaks on 5.x, so
# we pin the 4.48.x line -- >=4.48.0 clears CVE-2024-11392/-11393/-11394 while
# staying well under the 5.x break. The trailing -U upgrades patch transitive
# deps the base image / resolver pin to vulnerable versions (urllib3,
# cryptography, pillow, Brotli, setuptools, wheel). torch stays at the base
# image's 2.4.0: the only torch fix is 2.6.0, whose weights_only=True default
# breaks XTTS checkpoint loading, and the CVE is a torch.load RCE unreachable
# with Audicle's trusted XTTS model + WAV inputs.
COPY pyproject.toml ./
COPY config.py engine.py main.py ./
RUN pip install --no-cache-dir "transformers>=4.48.0,<4.49" \
    && pip install --no-cache-dir . \
    && pip install --no-cache-dir -U \
        "urllib3>=2.7.0" "cryptography>=46.0.5" "pillow>=12.2.0" \
        "Brotli>=1.2.0" "setuptools>=78.1.1" "wheel>=0.46.2"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=5 --start-period=120s \
    CMD curl -fsS http://localhost:8000/health/live || exit 1

# ``--factory`` so create_app() is called once with the default XTTS engine.
# ``--workers 1`` because XTTS holds GPU resources that don't survive forking;
# the asyncio.Lock inside the app serializes per-process /generate calls.
CMD ["uvicorn", "main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "300"]
