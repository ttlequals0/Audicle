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

- `POST /render` `{ "url": "...", "expand": true }` ->
  `{ "status": "ok|captcha|error", "html": "...", "clicks": N, "word_estimate": N }`
- `GET /health/live` -> `{ "ok": true, "version": "..." }`

The backend calls this only when a host is listed in `RENDER_HOSTS` or a solved
page still looks truncated, and never on the request path of a normal extraction.

## Layout

- `renderer.py` -- the result shape and the browser-agnostic helpers (expand-control
  matcher, CAPTCHA detector, public-host guard). Pure and unit-tested.
- `camoufox_renderer.py` -- the real Camoufox driver. Imported lazily so the app and
  its tests run without a browser installed.
- `main.py` -- the FastAPI app factory.

## Build and run

```
docker compose build render && docker compose up render
```

The image bakes the Camoufox browser at build time (`camoufox fetch`). DataDome is
probabilistic and IP-reputation aware, so a fraction of renders still hit a CAPTCHA
the sidecar cannot pass; those return `status: "captcha"` and the backend falls back
to the FlareSolverr partial.

## Tests

```
cd render && uv run pytest
```

Tests inject a fake renderer and exercise the pure helpers, so they need no
browser. The live click-through is validated manually against a real gated page
after deploy.
