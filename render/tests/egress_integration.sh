#!/bin/sh
set -eu

prefix="audicle-egress-test-$$"
fixture="$(mktemp -d "${TMPDIR:-/tmp}/audicle-egress-test.XXXXXX")"
control="$prefix-control"
public_net="$prefix-public"
private_net="$prefix-private"
proxy="$prefix-proxy"
renderer="$prefix-renderer"
public="$prefix-public-server"
private="$prefix-private-server"

cleanup() {
    rc="$?"
    if [ "$rc" -ne 0 ]; then
        docker logs "$renderer" 2>&1 || true
        docker logs "$proxy" 2>&1 || true
    fi
    for container in "$renderer" "$proxy" "$public" "$private"; do
        docker stop -t 1 "$container" >/dev/null 2>&1 || true
        docker rm "$container" >/dev/null 2>&1 || true
    done
    for network in "$control" "$public_net" "$private_net"; do
        docker network rm "$network" >/dev/null 2>&1 || true
    done
    rm -r -- "$fixture"
    exit "$rc"
}
trap cleanup EXIT

mkdir "$fixture/public" "$fixture/private"
cat > "$fixture/public/server.py" <<'PY'
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://10.77.0.10/private")
            self.end_headers()
            return
        body = b'<script src="http://10.77.0.10/script.js"></script><iframe src="http://10.77.0.10/frame"></iframe>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

HTTPServer(("0.0.0.0", 80), Handler).serve_forever()
PY
cat > "$fixture/private/server.py" <<'PY'
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        print(f"PRIVATE_REACHED {self.path}", flush=True)
        self.send_response(200)
        self.end_headers()

HTTPServer(("0.0.0.0", 80), Handler).serve_forever()
PY

docker build -t audicle-render-egress:integration render/egress-proxy >/dev/null
docker build -t audicle-render:integration render >/dev/null
# Internal like the deployed render-control network: no route out and no external DNS.
docker network create --internal --subnet 172.31.0.0/24 "$control" >/dev/null
docker network create --subnet 11.77.0.0/24 "$public_net" >/dev/null
docker network create --subnet 10.77.0.0/24 "$private_net" >/dev/null
docker run -d --name "$public" --network "$public_net" --ip 11.77.0.10 \
    -v "$fixture/public:/srv:ro" python:3.13-slim python /srv/server.py >/dev/null
docker run -d --name "$private" --network "$private_net" --ip 10.77.0.10 \
    -v "$fixture/private:/srv:ro" python:3.13-slim python /srv/server.py >/dev/null
# An explicit upstream makes Docker forward outside lookups from the proxy's namespace
# (as on a Linux host), so the proxy's firewall has to let them through.
docker run -d --name "$proxy" --network "$control" --ip 172.31.0.3 --dns 1.1.1.1 \
    --add-host rebound.test:10.77.0.10 --cap-drop ALL --cap-add NET_ADMIN \
    --cap-add SETUID --cap-add SETGID --cap-add SETPCAP \
    --security-opt no-new-privileges:true -e RENDER_CLIENT_IP=172.31.0.2 \
    audicle-render-egress:integration >/dev/null
docker network connect "$public_net" "$proxy"
docker network connect "$private_net" "$proxy"
docker run -d --name "$renderer" --network "$control" --ip 172.31.0.2 \
    --add-host rebound.test:11.77.0.10 --cap-drop ALL --cap-add NET_ADMIN \
    --cap-add SETUID --cap-add SETGID --cap-add SETPCAP \
    --security-opt no-new-privileges:true -e RENDER_PROXY_URL=http://172.31.0.3:3128 \
    -e RENDER_PROXY_IP=172.31.0.3 audicle-render:integration >/dev/null

# The internal network publishes no ports, so talk to the renderer from inside it as
# uid 1001, the only local client its firewall admits (as its healthcheck does).
renderer_curl() {
    docker exec "$renderer" setpriv --reuid=1001 --regid=1001 --clear-groups \
        --inh-caps=-all --bounding-set=-all --no-new-privs curl -fsS "$@"
}

ready=0
for _ in $(seq 1 60); do
    for container in "$proxy" "$renderer"; do
        test "$(docker inspect -f '{{.State.Running}}' "$container")" = true \
            || { echo "$container exited during startup" >&2; exit 1; }
    done
    if renderer_curl --max-time 2 http://127.0.0.1:8000/health/live >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
test "$ready" -eq 1 || { echo "renderer not ready after 60s" >&2; exit 1; }
for url in http://11.77.0.10/redirect http://11.77.0.10/subresources http://rebound.test/; do
    renderer_curl --max-time 30 -X POST http://127.0.0.1:8000/render \
        -H 'content-type: application/json' --data "{\"url\":\"$url\",\"expand\":false}" >/dev/null
done
# A public page must actually render through the proxy, with its name resolved there.
public_render="$(renderer_curl --max-time 60 -X POST http://127.0.0.1:8000/render \
    -H 'content-type: application/json' --data '{"url":"https://example.com","expand":false}')"
echo "$public_render" | grep -q '"status":"ok"' \
    || { echo "public render failed: $public_render" >&2; exit 1; }

test "$(docker logs "$private" 2>&1 | grep -c PRIVATE_REACHED || true)" -eq 0
test "$(docker exec "$proxy" iptables -L OUTPUT -v -n -x | awk '$9 == "10.0.0.0/8" {print $1}')" -gt 0
docker exec -d "$proxy" sh -c 'timeout 4 nc -u -l 127.0.0.1 9998 >/tmp/udp-received'
sleep 1
docker exec "$proxy" sh -c 'printf probe | setpriv --reuid=$(id -u proxy) --regid=$(id -g proxy) --clear-groups nc -u -w 1 127.0.0.1 9998 || true'
sleep 4
test "$(docker exec "$proxy" sh -c 'wc -c </tmp/udp-received')" -eq 0
docker exec "$proxy" setpriv --reuid="$(docker exec "$proxy" id -u proxy)" \
    --regid="$(docker exec "$proxy" id -g proxy)" --clear-groups \
    nc -6 -z -w 2 2606:4700:4700::1111 443 && exit 1 || true

# The root-only DNS rule (Docker's resolver) must not open DNS to the proxy user.
docker exec "$proxy" setpriv --reuid="$(docker exec "$proxy" id -u proxy)" \
    --regid="$(docker exec "$proxy" id -g proxy)" --clear-groups \
    nc -z -w 2 1.1.1.1 53 && exit 1 || true

echo "renderer egress boundary passed"
