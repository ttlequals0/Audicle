#!/usr/bin/env bash
# Supervises uvicorn (HTTP) and the worker. If either dies, the script exits
# non-zero so the container restart policy brings the whole stack back clean.
# A clean (exit 0) worker exit is still surfaced as 1 so `restart: unless-stopped`
# fires; the supervisor's contract is "both alive or container restarts."
set -euo pipefail

WEB_PID=""
WORKER_PID=""

cleanup() {
    trap '' TERM INT
    if [[ -n "${WEB_PID}" ]]; then
        kill -TERM "${WEB_PID}" 2>/dev/null || true
    fi
    if [[ -n "${WORKER_PID}" ]]; then
        kill -TERM "${WORKER_PID}" 2>/dev/null || true
    fi
    # Bounded wait so a child that ignores SIGTERM can't hold the container open.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if ! kill -0 "${WEB_PID}" 2>/dev/null && ! kill -0 "${WORKER_PID}" 2>/dev/null; then
            return
        fi
        sleep 1
    done
    if [[ -n "${WEB_PID}" ]]; then kill -KILL "${WEB_PID}" 2>/dev/null || true; fi
    if [[ -n "${WORKER_PID}" ]]; then kill -KILL "${WORKER_PID}" 2>/dev/null || true; fi
    wait 2>/dev/null || true
}
trap cleanup TERM INT

# Dual-stack when the container can bind "::" with V6ONLY off, else IPv4 only.
HOST=$(python -I -c 'import socket as s
try:
    k = s.socket(s.AF_INET6, s.SOCK_STREAM)
    k.bind(("::", 0))
    dual = k.getsockopt(s.IPPROTO_IPV6, s.IPV6_V6ONLY) == 0
except OSError:
    dual = False
print("::" if dual else "0.0.0.0")') || HOST=0.0.0.0
echo "audicle: listening on ${HOST}" >&2

uvicorn app.main:app \
    --host "${HOST}" \
    --port 8000 \
    --workers "${WEB_WORKERS:-2}" &
WEB_PID=$!

python -m app.worker &
WORKER_PID=$!

set +e
wait -n
EXIT=$?
set -e

echo "audicle: process group exit (web=${WEB_PID} worker=${WORKER_PID} exit=${EXIT})" >&2
cleanup
# Always exit non-zero when one child dies, even if it exited cleanly. Docker's
# `restart: unless-stopped` treats exit 0 as an intentional stop and won't bring
# the container back, but the supervisor's contract is "both alive or restart."
if [[ "${EXIT}" -eq 0 ]]; then
    exit 1
fi
exit "${EXIT}"
