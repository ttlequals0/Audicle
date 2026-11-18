# syntax=docker/dockerfile:1.7

# Slim Python base (no GPU -- the browser is CPU-only). Camoufox + a headful
# Firefox run under xvfb.
FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

# Empty stand-in for libcups2t64 (see the CUPS note below). Architecture: all, so one
# .deb serves amd64 and arm64. Built here so equivs never reaches the final image.
FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS cups-stub
RUN apt-get update \
    && apt-get install -y --no-install-recommends equivs \
    && mkdir /stub && cd /stub \
    && printf 'Package: audicle-libcups-stub\nVersion: 1.0\nArchitecture: all\nMaintainer: Audicle <noreply@example.invalid>\nProvides: libcups2t64 (= 2.4.99)\nConflicts: libcups2t64\nReplaces: libcups2t64\nDescription: empty stand-in for libcups2t64\n GTK depends on CUPS for printing only; this package ships no files.\n' > control \
    && equivs-build control

FROM python:3.14-slim AS runtime
COPY --from=uv /uv /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# xvfb for the headful browser's virtual display; xauth because xvfb-run needs it;
# Full apt upgrade pulls base-image security patches
# (same approach as the wrapper).
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends xvfb xauth ca-certificates iptables util-linux \
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

# From Debian sid until trixie ships the fixes. libsqlite3-0: CVE-2026-119373,
# CVE-2026-119374, CVE-2026-119379. libexpat1: CVE-2026-113351, CVE-2026-115506,
# CVE-2026-117610, CVE-2026-118370. xvfb and xserver-common: CVE-2026-109091,
# CVE-2026-109092, CVE-2026-109093, CVE-2026-109095, CVE-2026-109096,
# CVE-2026-109903, CVE-2026-93516. libxi6: CVE-2026-109089, CVE-2026-109090.
RUN printf 'Types: deb\nURIs: http://deb.debian.org/debian\nSuites: sid\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.pgp\n' > /etc/apt/sources.list.d/sid.sources \
    && printf 'Package: *\nPin: release a=unstable\nPin-Priority: 100\n' > /etc/apt/preferences.d/sid \
    && apt-get update \
    && apt-get install -y --no-install-recommends libsqlite3-0/sid libexpat1/sid xvfb/sid xserver-common/sid libxi6/sid \
    && rm -f /etc/apt/sources.list.d/sid.sources /etc/apt/preferences.d/sid \
    && rm -rf /var/lib/apt/lists/* \
    && for entry in 'libsqlite3-0 3.54.1-2' 'libexpat1 2.10.0-1' 'xvfb 2:21.1.26-1' 'xserver-common 2:21.1.26-1' 'libxi6 2:1.8.4-1'; do \
         set -- $entry; \
         dpkg --compare-versions "$(dpkg-query -W -f='${Version}' "$1")" ge "$2" || exit 1; \
       done

# GTK depends on libcups2t64 only for printing, which the renderer never does. The
# stub satisfies that dependency with no files, so the real libcups and the Kerberos,
# Avahi and GnuTLS libraries only it needed can be autoremoved with a consistent
# package database.
COPY --from=cups-stub /stub/audicle-libcups-stub_1.0_all.deb /tmp/audicle-libcups-stub.deb
RUN apt-get install -y --no-install-recommends /tmp/audicle-libcups-stub.deb \
    && dpkg --purge libcups2t64 \
    && apt-get autoremove -y --purge \
    && for pkg in libgssapi-krb5-2 libkrb5-3 libk5crypto3 libkrb5support0 libkeyutils1 \
         libcom-err2 libavahi-client3 libavahi-common3 libavahi-common-data \
         libgnutls30t64 libp11-kit0 libtasn1-6; do \
         case "$(dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null)" in \
           *'install ok installed') echo "ERROR: $pkg survived the CUPS removal" >&2; exit 1 ;; \
         esac; \
       done \
    && if find / -xdev -name 'libcups.so*' | grep -q .; then echo "ERROR: libcups files remain" >&2; exit 1; fi \
    && rm -f /tmp/audicle-libcups-stub.deb \
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
    CMD setpriv --reuid=1001 --regid=1001 --clear-groups --inh-caps=-all --bounding-set=-all --no-new-privs python -I -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health/live', timeout=4).status == 200 else 1)"

# entrypoint.sh starts Xvfb then exec's uvicorn (one worker -> one browser context
# at a time, bounded memory). See the script for why xvfb-run is not used here.
CMD ["./entrypoint.sh"]
