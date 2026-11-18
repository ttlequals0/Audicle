FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS tools

RUN apt-get update \
    && apt-get install -y --no-install-recommends util-linux

FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends tinyproxy-bin iptables ca-certificates netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# libsqlite3-0 from Debian sid clears CVE-2026-119373, CVE-2026-119374 and CVE-2026-119379.
# Drop this block when trixie ships the fix.
# The build enforces the fixed versions and fails if the sid install adds, removes or changes any other package.
RUN pkgs='libsqlite3-0=3.54.1-2' \
    && printf 'Types: deb\nURIs: http://deb.debian.org/debian\nSuites: sid\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.pgp\n' > /etc/apt/sources.list.d/sid.sources \
    && printf 'Package: *\nPin: release a=unstable\nPin-Priority: 100\n' > /etc/apt/preferences.d/sid \
    && apt-get update \
    && dpkg-query -W -f='${Package} ${Version}\n' > /tmp/pkgs.before \
    && apt-get install -y --no-install-recommends $(for e in $pkgs; do printf '%s/sid ' "${e%%=*}"; done) \
    && dpkg-query -W -f='${Package} ${Version}\n' > /tmp/pkgs.after \
    && rm -f /etc/apt/sources.list.d/sid.sources /etc/apt/preferences.d/sid \
    && rm -rf /var/lib/apt/lists/* \
    && for e in $pkgs; do \
         dpkg --compare-versions "$(dpkg-query -W -f='${Version}' "${e%%=*}")" ge "${e#*=}" || exit 1; \
       done \
    && unexpected="$(diff /tmp/pkgs.before /tmp/pkgs.after | sed -n 's/^[<>] \([^ ]*\) .*/\1/p' | sort -u | grep -vxF $(for e in $pkgs; do printf -- '-e %s ' "${e%%=*}"; done) || true)" \
    && if [ -n "$unexpected" ]; then echo "ERROR: the sid install changed other packages: $unexpected" >&2; exit 1; fi \
    && rm -f /tmp/pkgs.before /tmp/pkgs.after

COPY --from=tools /usr/bin/setpriv /usr/bin/setpriv
COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0555 /entrypoint.sh \
    && useradd --no-create-home --uid 1001 healthcheck

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD setpriv --reuid=1001 --regid=1001 --clear-groups --inh-caps=-all --bounding-set=-all --no-new-privs nc -z 127.0.0.1 3128

EXPOSE 3128
ENTRYPOINT ["/entrypoint.sh"]
