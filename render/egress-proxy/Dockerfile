FROM debian:trixie-slim@sha256:d7e12182ce18b85b93007c1dedf31f2d29e01ccf3182cc4017c709b6259bc132 AS tools

RUN apt-get update \
    && apt-get install -y --no-install-recommends util-linux

FROM debian:trixie-slim@sha256:d7e12182ce18b85b93007c1dedf31f2d29e01ccf3182cc4017c709b6259bc132

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
