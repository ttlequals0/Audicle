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
# A rebuild fails closed if a newer sid package needs a newer libc than trixie ships.
RUN printf 'Types: deb\nURIs: http://deb.debian.org/debian\nSuites: sid\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.pgp\n' > /etc/apt/sources.list.d/sid.sources \
    && printf 'Package: *\nPin: release a=unstable\nPin-Priority: 100\n' > /etc/apt/preferences.d/sid \
    && apt-get update \
    && apt-get install -y --no-install-recommends libsqlite3-0/sid \
    && rm -f /etc/apt/sources.list.d/sid.sources /etc/apt/preferences.d/sid \
    && rm -rf /var/lib/apt/lists/* \
    && for entry in 'libsqlite3-0 3.54.1-2'; do \
         set -- $entry; \
         dpkg --compare-versions "$(dpkg-query -W -f='${Version}' "$1")" ge "$2" || exit 1; \
       done

COPY --from=tools /usr/bin/setpriv /usr/bin/setpriv
COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0555 /entrypoint.sh \
    && useradd --no-create-home --uid 1001 healthcheck

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD setpriv --reuid=1001 --regid=1001 --clear-groups --inh-caps=-all --bounding-set=-all --no-new-privs nc -z 127.0.0.1 3128

EXPOSE 3128
ENTRYPOINT ["/entrypoint.sh"]
