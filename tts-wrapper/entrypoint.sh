#!/bin/sh
set -eu

# Dual-stack when the container can bind "::" with V6ONLY off, else IPv4 only.
HOST=$(python -I -c 'import socket as s
try:
    k = s.socket(s.AF_INET6, s.SOCK_STREAM)
    k.bind(("::", 0))
    dual = k.getsockopt(s.IPPROTO_IPV6, s.IPV6_V6ONLY) == 0
except OSError:
    dual = False
print("::" if dual else "0.0.0.0")') || HOST=0.0.0.0
echo "tts-wrapper: listening on ${HOST}" >&2

exec uvicorn main:create_app --factory --host "$HOST" --port 8000 --workers 1 --timeout-keep-alive 300
