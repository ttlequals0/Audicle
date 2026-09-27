#!/bin/sh
set -eu

PROXY_UID="$(id -u proxy)"
RENDER_CLIENT_IP="${RENDER_CLIENT_IP:?RENDER_CLIENT_IP is required}"
printf '%s\n' "$RENDER_CLIENT_IP" | awk -F. '
  NF != 4 { exit 1 }
  { for (i = 1; i <= 4; i++) if ($i !~ /^[0-9]+$/ || $i > 255) exit 1 }
' || { echo "invalid RENDER_CLIENT_IP" >&2; exit 1; }
sed "s/@RENDER_CLIENT_IP@/$RENDER_CLIENT_IP/g" /etc/tinyproxy/tinyproxy.conf > /tmp/tinyproxy.conf

iptables -P OUTPUT DROP
# First: replies on allowed connections. Docker's DNS answers from inside this netns,
# so its UDP reply must pass before the loopback UDP reject below.
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -o lo -p tcp --dport 3128 -m owner --uid-owner 1001 -j ACCEPT
iptables -A OUTPUT -o lo -p udp -m owner --uid-owner "$PROXY_UID" \
  -m conntrack --ctdir ORIGINAL --ctorigdst 127.0.0.11 --ctorigdstport 53 -j ACCEPT
iptables -A OUTPUT -o lo -p tcp -m owner --uid-owner "$PROXY_UID" \
  -m conntrack --ctdir ORIGINAL --ctorigdst 127.0.0.11 --ctorigdstport 53 -j ACCEPT
iptables -A OUTPUT -o lo -p udp -j REJECT
# On a Linux host Docker's embedded resolver forwards outside lookups from this
# namespace, as root, to the host's nameservers (often a LAN address, rejected below).
# Only root's DNS gets through: after the exec below, tinyproxy runs as proxy and
# dockerd's resolver is the only root left in this namespace.
iptables -A OUTPUT -p udp --dport 53 -m owner --uid-owner 0 -j ACCEPT
iptables -A OUTPUT -p tcp --dport 53 -m owner --uid-owner 0 -j ACCEPT
for cidr in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.0.2.0/24 192.88.99.0/24 192.168.0.0/16 198.18.0.0/15 198.51.100.0/24 203.0.113.0/24 224.0.0.0/4 240.0.0.0/4; do
  iptables -A OUTPUT -d "$cidr" -j REJECT
done
iptables -A OUTPUT -p tcp -m multiport --dports 80,443 -m owner --uid-owner "$PROXY_UID" -j ACCEPT
iptables -A OUTPUT -j REJECT

ip6tables -P OUTPUT DROP
ip6tables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -A OUTPUT -p udp --dport 53 -m owner --uid-owner 0 -j ACCEPT
ip6tables -A OUTPUT -p tcp --dport 53 -m owner --uid-owner 0 -j ACCEPT
ip6tables -A OUTPUT -j REJECT

exec setpriv --reuid="$PROXY_UID" --regid="$PROXY_UID" --clear-groups \
  --inh-caps=-all --ambient-caps=-all --bounding-set=-all --no-new-privs \
  tinyproxy -d -c /tmp/tinyproxy.conf
