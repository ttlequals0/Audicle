# syntax=docker/dockerfile:1.7

# Slim Python base (no GPU -- the browser is CPU-only). Camoufox + a headful
# Firefox run under xvfb.
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# xvfb for the headful browser's virtual display; xauth because xvfb-run needs it;
# curl for the healthcheck. Full apt upgrade pulls base-image security patches
# (same approach as the wrapper).
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends xvfb xauth curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY main.py renderer.py camoufox_renderer.py entrypoint.sh ./

# Install the app + the browser extra, then let Playwright pull Firefox's OS
# dependencies (it maintains the correct per-distro package list, so we don't
# hand-maintain a fragile apt list).
RUN pip install --no-cache-dir ".[render]" \
    && python -m playwright install-deps firefox \
    && rm -rf /var/lib/apt/lists/*

# Run the browser as a non-root user (Firefox is happier non-root) and bake the
# Camoufox browser into that user's home so it ships in the image.
RUN useradd --create-home --uid 1000 render \
    && chmod +x entrypoint.sh \
    && chown -R render:render /app
USER render
ENV HOME=/home/render
RUN python -m camoufox fetch

# Single-source version: the build passes the repo-root VERSION value here since
# this build context is render/ and can't read the root file. main.py reads
# AUDICLE_RENDER_VERSION.
ARG RENDER_VERSION=0.0.0
ENV AUDICLE_RENDER_VERSION=${RENDER_VERSION}

EXPOSE 8000

# Generous start-period: the first request still pays browser spin-up, but the
# browser is already baked, so liveness comes up quickly.
HEALTHCHECK --interval=30s --timeout=10s --retries=5 --start-period=60s \
    CMD curl -fsS http://localhost:8000/health/live || exit 1

# entrypoint.sh starts Xvfb then exec's uvicorn (one worker -> one browser context
# at a time, bounded memory). See the script for why xvfb-run is not used here.
CMD ["./entrypoint.sh"]
