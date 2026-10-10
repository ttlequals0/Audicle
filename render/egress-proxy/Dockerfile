FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS tools

RUN apt-get update \
    && apt-get install -y --no-install-recommends util-linux

FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends tinyproxy-bin iptables ca-certificates netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

COPY --from=tools /usr/bin/setpriv /usr/bin/setpriv
COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0555 /entrypoint.sh \
    && useradd --no-create-home --uid 1001 healthcheck

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD setpriv --reuid=1001 --regid=1001 --clear-groups --inh-caps=-all --bounding-set=-all --no-new-privs nc -z 127.0.0.1 3128

EXPOSE 3128
ENTRYPOINT ["/entrypoint.sh"]
