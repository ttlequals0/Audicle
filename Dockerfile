# syntax=docker/dockerfile:1.7

# ---- Stage 1: frontend builder (stubbed for Phase 1; real Vite build lands in Phase 11). ----
FROM node:20-alpine AS frontend
WORKDIR /build
RUN mkdir -p dist && \
    printf '<!doctype html><meta charset="utf-8"><title>Audicle</title>\n<p>UI lands in Phase 11. The API is at <a href="/api/v1/docs">/api/v1/docs</a>.</p>\n' > dist/index.html

# ---- Stage 2: uv builder ----
# Pin uv to a single tag for reproducibility; bump deliberately.
FROM ghcr.io/astral-sh/uv:0.5.8 AS uv

# ---- Stage 3: Python runtime ----
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         curl \
         ca-certificates \
         ffmpeg \
         libsndfile1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 audicle

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first so source edits don't bust the layer cache.
# --frozen requires uv.lock; if it's missing or stale the build fails loudly
# (reproducibility is the whole point of committing the lockfile).
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

COPY backend/app ./app
COPY --from=frontend /build/dist ./static/ui
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh && chown -R audicle:audicle /app

USER audicle
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=20s \
    CMD curl -fsS http://localhost:8000/health/live || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
