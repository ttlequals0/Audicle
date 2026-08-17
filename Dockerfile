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
FROM ghcr.io/astral-sh/uv:0.12.4 AS uv

# ---- Stage 2b: static ffmpeg ----
# Pinned BtbN GPL static build, sha256-verified. Audicle only subprocesses the
# ffmpeg binary, so a static binary is a drop-in -- and it removes apt ffmpeg's
# mesa/GL/SDL/pango/mbedcrypto dependency tree. ffprobe/ffplay are not shipped
# (nothing uses them). The tarball (BtbN autobuild-2026-07-10-13-44) is mirrored
# to this repo's releases: BtbN deletes daily autobuilds after ~14 days. To
# bump: mirror the new BtbN asset to a new release tag, update URL + sha256 together.
FROM debian:trixie-slim AS ffmpeg
# The mirrored tarball is amd64-only (apt ffmpeg was arch-native). Fail fast
# with a clear message on other arches instead of an exec-format error later
# or a silently mixed-arch image under qemu emulation.
ARG TARGETARCH
RUN [ "$TARGETARCH" = "amd64" ] || { \
      echo "ERROR: the pinned static ffmpeg is amd64-only; build with --platform linux/amd64" >&2; \
      exit 1; \
    }
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
    && useradd --create-home --uid 1000 audicle \
    # The runtime venv is built by uv; the base image's pip (whose vendored
    # msgpack/pkg_resources copies keep sprouting CVEs) is unused -- remove it.
    && rm -rf /usr/local/lib/python3.14/site-packages/pip* \
       /usr/local/lib/python3.14/ensurepip

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
