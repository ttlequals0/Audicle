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
    TTS_HOME=/root/.cache/huggingface/tts

# OS deps: curl for healthchecks; libsndfile for soundfile/coqui-tts WAV IO.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps from pyproject.toml so this image stays in lockstep with
# the declared dependency set; no parallel hand-written list to drift.
# The transformers cap (coqui-tts 0.24 breaks on transformers 5.x; see the note
# in pyproject.toml) is applied here rather than in the manifest.
COPY pyproject.toml ./
COPY config.py engine.py main.py ./
RUN pip install --no-cache-dir "transformers>=4.43.0,<4.47" \
    && pip install --no-cache-dir .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=5 --start-period=120s \
    CMD curl -fsS http://localhost:8000/health || exit 1

# ``--factory`` so create_app() is called once with the default XTTS engine.
# ``--workers 1`` because XTTS holds GPU resources that don't survive forking;
# the asyncio.Lock inside the app serializes per-process /generate calls.
CMD ["uvicorn", "main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "300"]
