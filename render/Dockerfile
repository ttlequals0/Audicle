# syntax=docker/dockerfile:1.7

# Slim Python base (no GPU -- the browser is CPU-only). Camoufox + a headful
# Firefox run under xvfb.
FROM ghcr.io/astral-sh/uv:0.12.17 AS uv
FROM python:3.12-slim AS runtime
COPY --from=uv /uv /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# xvfb for the headful browser's virtual display; xauth because xvfb-run needs it;
# curl for the healthcheck. Full apt upgrade pulls base-image security patches
# (same approach as the wrapper).
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends xvfb xauth curl ca-certificates iptables util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY main.py renderer.py camoufox_renderer.py log_config.py entrypoint.sh ./

# Install the app + the browser extra, then let Playwright pull Firefox's OS
# dependencies (it maintains the correct per-distro package list, so we don't
# hand-maintain a fragile apt list).
RUN uv export --locked --no-dev --extra render --no-emit-project --output-file /tmp/render-requirements.txt \
    && uv pip install --system --no-cache --require-hashes -r /tmp/render-requirements.txt \
    && uv pip install --system --no-cache --no-deps . \
    && rm /tmp/render-requirements.txt \
    && python -m playwright install-deps firefox \
    && uv pip uninstall --system pip \
    && python -c 'import shutil, sysconfig; from pathlib import Path; shutil.rmtree(Path(sysconfig.get_path("stdlib")) / "ensurepip")' \
    && rm -rf /var/lib/apt/lists/*

# Run the browser as a non-root user (Firefox is happier non-root) and bake the
# Camoufox browser into that user's home so it ships in the image.
RUN useradd --create-home --uid 1000 render \
    && useradd --no-create-home --uid 1001 healthcheck \
    && install -d -o render -g render /home/render/.cache /home/render/.cache/camoufox \
    && install -d -m 1777 /tmp/.X11-unix \
    && chmod +x entrypoint.sh \
    && chown -R render:render /app
ENV HOME=/home/render
# `camoufox fetch` exits 0 even when extraction fails (e.g. a full disk), so verify the
# browser binary landed; otherwise the build "succeeds" with a renderer that can't start.
RUN setpriv --reuid=1000 --regid=1000 --init-groups python -m camoufox fetch \
    && find /home/render/.cache/camoufox/browsers -name camoufox-bin -type f -perm -u+x | grep -q . \
    || { echo "ERROR: Camoufox installation failed" >&2; exit 1; }

EXPOSE 8000

# Generous start-period: the first request still pays browser spin-up, but the
# browser is already baked, so liveness comes up quickly.
HEALTHCHECK --interval=30s --timeout=10s --retries=5 --start-period=60s \
    CMD setpriv --reuid=1001 --regid=1001 --clear-groups --inh-caps=-all --bounding-set=-all --no-new-privs curl -fsS http://localhost:8000/health/live || exit 1

# entrypoint.sh starts Xvfb then exec's uvicorn (one worker -> one browser context
# at a time, bounded memory). See the script for why xvfb-run is not used here.
CMD ["./entrypoint.sh"]
