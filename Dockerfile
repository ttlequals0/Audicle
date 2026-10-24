# syntax=docker/dockerfile:1.7

# Global arg for the ffmpeg stages below (see Stage 2b). TARGETARCH is a
# BuildKit built-in; declaring it here would blank it in FROM lines.
ARG FFMPEG_MIRROR_BASE=https://github.com/ttlequals0/Audicle/releases/download/ffmpeg-static-n9.0.2-35-g9a9b70bb8c

# ---- Stage 1: frontend builder (Vite + React + Tailwind). ----
FROM node:26-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json* ./
# ``--ignore-scripts`` blocks dependency lifecycle hooks from running with
# the npm install -- defense-in-depth against malicious postinstall scripts
# in transitive deps.
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend/ ./
COPY branding/tokens.json branding/tokens.css /build/branding/
COPY scripts/sync_design_tokens.mjs /build/scripts/
RUN npm run build

# ---- Stage 2: uv builder ----
# Pin uv to a single tag for reproducibility; bump deliberately.
FROM ghcr.io/astral-sh/uv:0.13.1 AS uv

# ---- Stage 2b: static ffmpeg ----
# Pinned BtbN GPL static build per arch, sha256-verified. Audicle only
# subprocesses the ffmpeg binary, so a static binary is a drop-in, and it
# removes apt ffmpeg's mesa/GL/SDL/pango/mbedcrypto dependency tree. ffprobe/
# ffplay are not shipped (nothing uses them). The tarballs (BtbN
# autobuild-2026-10-24-13-02, linux64 and linuxarm64) are mirrored to this
# repo's releases: BtbN deletes daily autobuilds after ~14 days. To bump:
# mirror both new BtbN assets to a new release tag, update the tag/version in
# FFMPEG_MIRROR_BASE and both URLs and both sha256s together.
FROM debian:trixie-slim AS ffmpeg-amd64
ARG FFMPEG_MIRROR_BASE
ADD --checksum=sha256:ddffcf6e3d537dd9e0039f0635261be40dd9bfdafb2ea85970173e928e024e50 \
    ${FFMPEG_MIRROR_BASE}/ffmpeg-n9.0.2-35-g9a9b70bb8c-linux64-gpl-9.0.tar.xz \
    /tmp/ffmpeg.tar.xz

FROM debian:trixie-slim AS ffmpeg-arm64
ARG FFMPEG_MIRROR_BASE
ADD --checksum=sha256:710ac178fffa266d6b197a9e3cca4d2c62f35bed70d5cf99762d40aa8a5791f9 \
    ${FFMPEG_MIRROR_BASE}/ffmpeg-n9.0.2-35-g9a9b70bb8c-linuxarm64-gpl-9.0.tar.xz \
    /tmp/ffmpeg.tar.xz

# An arch with no ffmpeg-<arch> stage fails here ("ffmpeg-<arch>: not found").
FROM ffmpeg-${TARGETARCH} AS ffmpeg
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
    && useradd --create-home --uid 1000 audicle \
    # The runtime venv is built by uv; the base image's pip (whose vendored
    # msgpack/pkg_resources copies keep sprouting CVEs) is unused -- remove it.
    && rm -rf /usr/local/lib/python3.14/site-packages/pip* \
       /usr/local/lib/python3.14/ensurepip

COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first so source edits don't bust the layer cache.
# --locked requires uv.lock; if it's missing or stale the build fails loudly
# (reproducibility is the whole point of committing the lockfile).
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --locked

COPY backend/app ./app
COPY VERSION ./
COPY --from=frontend /build/frontend/dist ./static/ui
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh && chown -R audicle:audicle /app

USER audicle
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=20s \
    CMD curl -fsS http://localhost:8000/health/live || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
