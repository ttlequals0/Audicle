# Audicle render sidecar

A small internal service that fetches the full article body for pages gated
behind an "EXPAND TO CONTINUE READING" click (inc.com and similar, which run
DataDome). FlareSolverr clears the JS challenge but runs a headless browser that
cannot click, so it only returns the visible front half. This sidecar loads the
page in a headful Camoufox (anti-fingerprint Firefox) under xvfb, clicks the
expander until the body stops growing, and returns the final HTML. The backend
turns that HTML into article markdown with the same trafilatura path it uses for
FlareSolverr, and keeps it only when it is longer than what the cascade already
had.

## API

| Method | Path | Purpose |
|--------|------|---------|
| POST | /render | Body: `{url, expand}` (`expand` defaults to true). Returns `{status, html, clicks, word_estimate}` where `status` is `ok`, `captcha` or `error`. |
| GET | /health/live | `{ok, version}` |
| GET, PUT | /runtime-config | Read the render defaults and effective values, or change `LOG_LEVEL` and `LOG_FORMAT` live. |

`/render` also accepts optional `email`, `cookies` and `budget_seconds` fields and
per-request overrides of the `RENDER_*` tuning values (navigation, click and settle
timeouts, attempts, total budget). The backend sends these from Settings; the
defaults and units are in [Renderer settings](../docs/environment-variables.md#renderer-settings).

The backend calls this only for a host whose Site-override rule is the `render`
strategy (or a solved page that still looks truncated), and never on the request
path of a normal extraction.

## Network

The container listens on port 8000, dual-stack (IPv6 plus IPv4) when it can bind
`::`, and IPv4 only (`0.0.0.0`) otherwise. `listen.py` opens the socket at start,
logs the address, and hands it to uvicorn. Never publish this port; the backend reaches it on the internal
network.

The browser has no direct route out. `entrypoint.sh` runs as root with only the
capabilities it needs, installs a default-drop `iptables` OUTPUT policy, then drops
privileges before starting Xvfb and uvicorn as uid 1000 (`render`). Uid 1000 may
reach only the egress proxy (`RENDER_PROXY_IP`, port `RENDER_PROXY_PORT`, default
3128) and Docker's embedded DNS; all IPv6 egress is dropped. Uid 1001
(`healthcheck`) may only reach port 8000 on loopback.

The proxy is the separate `render-egress` image (`egress-proxy/`, tinyproxy):

- It accepts connections only from `RENDER_CLIENT_IP` and listens on IPv4. The leg
  from this service to the proxy is IPv4-only by design: a pinned address on an
  internal Docker network.
- Its own firewall rejects loopback, private, link-local, shared, multicast and
  other special-use IPv4 ranges, and allows outbound TCP 80 and 443 only. A public
  name that resolves to a private address is therefore refused at the proxy.
- The proxy runs as the `proxy` user with all capabilities dropped; only root's
  DNS forwarding is allowed besides web traffic.

`RENDER_PROXY_IP`, `RENDER_PROXY_PORT`, `RENDER_NETWORK_SUBNET` and
`RENDER_CLIENT_IP` are described in
[Renderer settings](../docs/environment-variables.md#renderer-settings). The
compose service also needs `shm_size: "1gb"`; headful Firefox crashes with the
64 MB default.

## Layout

- `renderer.py`: the result shape and the browser-agnostic helpers (expand-control
  matcher, CAPTCHA detector, public-host guard). Pure and unit-tested.
- `camoufox_renderer.py`: the real Camoufox driver. Imported lazily so the app and
  its tests run without a browser installed.
- `main.py`: the FastAPI app factory.
- `log_config.py`: logging setup.
- `entrypoint.sh`: firewall rules, Xvfb, uvicorn.
- `egress-proxy/`: the `render-egress` image.

## Build and run

The `render` service pulls the published image and has no `build:` stanza, so build
directly:

```
docker build -t audicle-render:dev -f render/Dockerfile render/
```

Then pin that tag in `docker-compose.yml` and `docker compose up render`. The
`render` service depends on `render-egress`.

The egress proxy has its own image:

```
docker build -t audicle-render-egress:dev render/egress-proxy/
```

The image bakes the Camoufox browser at build time (`camoufox fetch`). If that
step hits the GitHub API rate limit, pass a token as an optional BuildKit secret:
`GITHUB_TOKEN=<token> docker build --secret id=github_token,env=GITHUB_TOKEN ... render/`.
It is mounted for that step only and is not stored in the image. DataDome is
probabilistic and IP-reputation aware, so a fraction of renders still hit a CAPTCHA
the sidecar cannot pass; those return `status: "captcha"` and the backend keeps
what the cascade already had.

## Tests

```
cd render && uv run pytest
```

Tests inject a fake renderer and exercise the pure helpers, so they need no
browser. The live click-through is validated manually against a real gated page
after deploy.

`sh render/tests/egress_integration.sh` (needs Docker) starts the proxy and a renderer
container against local public and private servers and checks the firewall.
