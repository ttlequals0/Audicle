#!/bin/sh
set -e

PROXY_IP="${RENDER_PROXY_IP:?RENDER_PROXY_IP is required}"
PROXY_PORT="${RENDER_PROXY_PORT:-3128}"
python - "$PROXY_IP" "$PROXY_PORT" <<'PY'
import ipaddress
import sys

ipaddress.IPv4Address(sys.argv[1])
port = int(sys.argv[2])
if not 1 <= port <= 65535:
    raise ValueError("invalid proxy port")
PY

iptables -P OUTPUT DROP
# First: replies on allowed connections. Docker's DNS answers from inside this netns,
# so its UDP reply must pass before the loopback UDP reject below.
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -p tcp -d "$PROXY_IP" --dport "$PROXY_PORT" -m owner --uid-owner 1000 -j ACCEPT
iptables -A OUTPUT -o lo -p udp -m owner --uid-owner 1000 \
  -m conntrack --ctdir ORIGINAL --ctorigdst 127.0.0.11 --ctorigdstport 53 -j ACCEPT
iptables -A OUTPUT -o lo -p tcp -m owner --uid-owner 1000 \
  -m conntrack --ctdir ORIGINAL --ctorigdst 127.0.0.11 --ctorigdstport 53 -j ACCEPT
iptables -A OUTPUT -o lo -p tcp --dport 8000 -m owner --uid-owner 1001 -j ACCEPT
iptables -A OUTPUT -o lo -p udp -j REJECT
iptables -A OUTPUT -j REJECT
ip6tables -P OUTPUT DROP
ip6tables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -A OUTPUT -o lo -p tcp --dport 8000 -m owner --uid-owner 1001 -j ACCEPT
ip6tables -A OUTPUT -j REJECT

# Headful Camoufox needs an X display. Start Xvfb in the background, point DISPLAY
# at it, then exec uvicorn so it becomes PID 1 and handles signals. xvfb-run as
# PID 1 does not reliably run its command in a container (it traps signals as a
# shell script and the command never starts), so we drive Xvfb directly.
setpriv --reuid=1000 --regid=1000 --init-groups --inh-caps=-all --bounding-set=-all --no-new-privs \
  Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp &
export DISPLAY=:99

exec setpriv --reuid=1000 --regid=1000 --init-groups --inh-caps=-all \
  --bounding-set=-all --no-new-privs \
  uvicorn main:create_app --factory --host 0.0.0.0 --port 8000 --workers 1
