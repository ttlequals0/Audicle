# syntax=docker/dockerfile:1.7

# ---- Stage 1: frontend builder (Vite + React + Tailwind). ----
FROM node:26-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
# ``--ignore-scripts`` blocks dependency lifecycle hooks from running with
# the npm install -- defense-in-depth against malicious postinstall scripts
# in transitive deps.
RUN npm install --ignore-scripts --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: uv builder ----
# Pin uv to a single tag for reproducibility; bump deliberately.
FROM ghcr.io/astral-sh/uv:0.11.26 AS uv

# ---- Stage 2b: static ffmpeg ----
# Pinned BtbN GPL static build, sha256-verified. Audicle only subprocesses the
# ffmpeg binary (normalize/encode in audio.py, -version probe in health.py), so
# a static binary is a drop-in -- and it removes apt ffmpeg's mesa/GL/SDL/pango/
# mbedcrypto dependency tree, the bulk of this image's CVE surface. ffprobe and
# ffplay are not shipped (nothing uses them). Bump the tag/asset/sha256 together.
#
# The tarball is a BtbN build (autobuild-2026-07-10-13-44) mirrored to this
# repo's releases: BtbN deletes daily autobuilds after ~14 days, which would
# break clean rebuilds. To bump: mirror the new BtbN asset to a new release
# tag here, then update URL + sha256 together.
FROM debian:trixie-slim AS ffmpeg
ADD --checksum=sha256:8a3a9d2919b687602dfed430e0397779405589357e7108950e506a3291af9371 \
    https://github.com/ttlequals0/Audicle/releases/download/ffmpeg-static-n8.1.2-22-g94138f6973/ffmpeg-n8.1.2-22-g94138f6973-linux64-gpl-8.1.tar.xz \
    /tmp/ffmpeg.tar.xz
RUN apt-get update && apt-get install -y --no-install-recommends xz-utils \
    && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp \
    && mv /tmp/ffmpeg-*/bin/ffmpeg /ffmpeg \
    && /ffmpeg -version \
    && rm -rf /tmp/ffmpeg.tar.xz /tmp/ffmpeg-* /var/lib/apt/lists/*

# ---- Stage 3: Python runtime ----
FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
         curl \
         ca-certificates \
         libsndfile1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 audicle

COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first so source edits don't bust the layer cache.
# --frozen requires uv.lock; if it's missing or stale the build fails loudly
# (reproducibility is the whole point of committing the lockfile).
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

COPY backend/app ./app
COPY VERSION ./
COPY --from=frontend /build/dist ./static/ui
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh && chown -R audicle:audicle /app

USER audicle
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=20s \
    CMD curl -fsS http://localhost:8000/health/live || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
