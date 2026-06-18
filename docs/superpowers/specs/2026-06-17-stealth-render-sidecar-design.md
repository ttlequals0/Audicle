# Stealth-render sidecar design (0.38.0)

**Goal:** Retrieve the full article body for sites that hide the back half behind an
interactive "EXPAND TO CONTINUE READING" click (inc.com and lookalikes), which the
existing FlareSolverr fallback cannot reach because it never clicks.

**Status:** Approved design. Next step is an implementation plan.

## Problem

The extraction cascade already routes a blocked direct fetch (403/429) through
FlareSolverr. For inc.com this clears the DataDome JS challenge and returns the visible
top of the article, but the rest of the body only loads after a real browser clicks an
"EXPAND TO CONTINUE READING" control. FlareSolverr runs a headless browser that cannot
click, so the cascade accepts the partial.

Logs from a live run (job `f9d02c90`, inc.com CoreWeave article) make the failure mode
concrete:

- `extraction_blocked_primary` -> direct fetch 403, routes to fallback (working as built).
- `extraction_fallback_used` -> FlareSolverr returns `markdown_chars: 2567`.
- `chunk_complete` -> `total_words: 402`, a ~148s episode.

The full article is roughly 770 words (confirmed by a feasibility prototype that clicked
the expander). So readers get about half. The partial is also invisible: 2567 chars clears
the 500-char floor, so the pipeline treats it as a clean success. Nothing flags it as
truncated.

## Approach

Add a small sidecar container that runs a stealth headful browser, loads the page, clicks
the expander until the body stops growing, and returns the final HTML. The backend feeds
that HTML through the same trafilatura path FlareSolverr already uses, so the rest of the
pipeline is unchanged.

The cascade today is first-above-floor-wins: the fallback loop returns the first attempt
whose body clears the floor (`extraction.py`, `if alt_chars >= accept_floor: return alt`).
FlareSolverr's inc.com partial (2567 chars) clears the 500-char floor, so the loop stops
there - any attempt appended after it never runs, and there is no longest-body comparison.
So render cannot be just another loop attempt. Instead it runs as a **post-cascade
enrichment step**: once the cascade has settled on a result, if render is configured and the
result looks like it needs expanding, call the sidecar and keep its body only if it is
longer. This sidesteps the short-circuit and naturally handles the invisible-partial case
(an above-floor result that is still truncated).

The engine is **Camoufox** (a C++-patched stealth Firefox). Current benchmarks put it ahead
of nodriver and Patchright against DataDome specifically; nodriver (used in the throwaway
prototype) is reported to get blocked quickly because it does not simulate human input. The
engine lives behind the sidecar's HTTP boundary, so it can be swapped later without touching
the backend.

DataDome is probabilistic and IP-reputation aware. Even on a clean residential IP a fraction
of attempts will hit a CAPTCHA the sidecar cannot solve. Those degrade gracefully to the
FlareSolverr partial and are logged. No proxy support in this build (the host runs on a
residential IP); a `PROXY_URL` env hook is a one-line future add.

## Components

### 1. Sidecar service (`render/`, new top-level dir, sibling to `tts-wrapper/`)

A FastAPI app plus Camoufox plus xvfb in one image, internal-only (never edge-exposed).
One real endpoint:

- `POST /render` with `{ "url": str, "expand": bool }` ->
  `{ "status": "ok" | "blocked" | "captcha" | "error", "html": str, "clicks": int, "word_estimate": int }`
- `GET /health/live` -> `{ "ok": true, "version": "..." }`

Behavior per request:

1. Launch a fresh Camoufox context (stateless, fresh fingerprint each call). Low volume
   (a few articles a day) makes per-request launch fine and avoids session tracking.
2. Navigate, let the DataDome check settle.
3. If `expand`, run the expand loop (component 2).
4. Detect a CAPTCHA/block using the markers already known from FlareSolverr detection
   (`verification required`, `slide right to secure`, `unusual activity from your device`,
   etc.). If present and the body is still short, return `status: "captcha"` / `"blocked"`
   with no usable HTML.
5. Otherwise serialize the final DOM (`document.documentElement.outerHTML`), cap it at the
   backend's `MAX_HTML_CHARS` equivalent, and return `status: "ok"` with the HTML.

Defense in depth: the sidecar refuses non-public/loopback/private hosts even though the
backend validates first.

### 2. Expand loop (inside the sidecar, generic - no per-host selectors)

Find visible, clickable elements whose trimmed text matches
`/expand|continue reading|read more|show more/i`. Click the first, wait for the document
text length to grow (bounded wait), repeat until no matching control remains or a click cap
(default 3) is hit. Generic by design so inc.com and similar sites work with no per-site
configuration. The click count and a word estimate ride back in the response for logging.

### 3. Backend client (`backend/app/services/render.py`, mirrors `flaresolverr.py`)

`async def fetch(url: str, settings: Settings) -> ExtractionResult | None`

- POST to `settings.RENDER_URL` with a timeout derived from `RENDER_TIMEOUT_SECONDS`.
- On a non-`ok` status, or transport error, or non-JSON: log a warning
  (`render_blocked` / `render_captcha` / `render_unreachable`) and return `None`.
- On `ok`: run `html_to_markdown(html)`; if empty, log `render_empty_extract` and return
  `None`; otherwise return `ExtractionResult(markdown, metadata)`.
- Never raises, matching the FlareSolverr client contract.

### 4. Cascade wiring (`extraction.py` only - the source-fallback cascade is untouched)

Render is a post-cascade enrichment step, not a loop attempt (see Approach), and it stays
orthogonal to the source-fallback rules so it cannot disturb a host's existing recipe. The
logs show inc.com matches the global googlebot rule and FlareSolverr auto-escalates on the
403 to produce the baseline partial; enrichment must preserve that, not replace it.

- `extraction.py`: wrap the existing cascade. Move the current orchestrator body to an inner
  function that returns the cascade result; `extract()` calls it, then passes the result
  through `_maybe_render_full(result, url, settings)` before returning. The inner function
  keeps every existing return/raise path; enrichment only runs on a successful result (a
  raised `ExtractionTooShortError` propagates unchanged - no enrichment on total failure).
- `_maybe_render_full`: a no-op unless `RENDER_URL` is set. It runs render when either the
  URL's host is in `RENDER_HOSTS`, or `looks_truncated(result)` is true. It calls
  `render.fetch`, and replaces the result only when the render body is strictly longer. A
  shorter or `None` render result keeps the cascade's body, so a broken click never loses the
  partial. Host match is a simple suffix test (`host == h or host.endswith("." + h)`) so
  `inc.com` covers `www.inc.com`.
- `looks_truncated(result)`: scans the available result text for the expand markers
  (`expand to continue reading`, `continue reading`, `read more`). Best-effort host-agnostic
  net. Trafilatura may strip a button's text from the extracted markdown, so `RENDER_HOSTS`
  is the reliable trigger for known sites and marker detection is the bonus net for the rest.

### 5. Config (`backend/app/config.py`)

- `RENDER_URL: str = ""` (empty -> engine off; the compose service sets it so the standard
  stack has it on, custom/minimal deploys stay off).
- `RENDER_TIMEOUT_SECONDS: float = 90.0` (browser launch + DataDome settle + expand clicks
  need a generous budget; connect stays short so an unreachable sidecar fails fast).
- `RENDER_HOSTS: str = "inc.com"` (comma-separated host list that always routes to render
  enrichment; the motivating site works out of the box, operators can add more). Env-tunable,
  like the FlareSolverr and webhook settings. DB/UI management of this list is a future
  enhancement, not in this build.

### 6. Deployment

- New image `ttlequals0/audicle-render`, new `docker-compose.yml` service `audicle-render`
  (build context `render/`, sets `RENDER_URL` for the app), pinned to `${BUILD_VERSION}`
  like the other two images. This makes three images to build, scan, push, and version each
  release - a real and acknowledged cost.
- `render/Dockerfile`: a Python base with Camoufox and xvfb, `xvfb-run` wrapping uvicorn.
  Version injected by `--build-arg RENDER_VERSION=$(cat VERSION)` like the wrapper.

## Data flow

```
extract(url)
  cascade (unchanged):
    direct_fetch 403 -> ExtractionBlockedError
    -> FlareSolverr -> partial markdown (2567 chars), clears floor -> cascade returns it
  enrichment (_maybe_render_full, new):
    RENDER_URL set AND (host in RENDER_HOSTS OR looks_truncated(partial))
    -> render.fetch -> POST sidecar /render {url, expand:true}
         -> Camoufox loads, clears DataDome, clicks expand x N -> full DOM HTML
       -> html_to_markdown -> full markdown (~770 words)
    -> render body longer than partial -> use it; else keep partial
  pipeline continues unchanged
  (sidecar captcha/blocked/error -> fetch returns None -> keep partial, logged)
```

## Error handling

- Sidecar unreachable / timeout / non-JSON -> `render.fetch` returns `None`, cascade keeps
  the prior result. Logged.
- CAPTCHA / block detected -> `status: "captcha"`/`"blocked"`, `None`, keep partial. Logged
  with host so a run reads as "render blocked by CAPTCHA," not a silent partial.
- Expand click breaks the page or shrinks it -> render result is shorter, so the
  longer-body-wins rule discards it. No regression versus the FlareSolverr partial.
- Oversize DOM -> capped at the `MAX_HTML_CHARS` equivalent before serialization.

## Testing

- Backend unit (`backend/tests/test_render.py`, mirrors `test_flaresolverr.py`): `fetch`
  happy path (mock sidecar HTML -> trafilatura markdown), each of `blocked`/`captcha`/`error`
  -> `None` plus the right log event, `looks_truncated` marker hits and misses.
- Cascade unit (extend `test_extraction.py`): `_maybe_render_full` calls render when the host
  is in `RENDER_HOSTS` or `looks_truncated` is true, and skips it otherwise; a longer render
  body replaces the cascade result while a shorter or `None` one keeps the partial; enrichment
  is a no-op when `RENDER_URL` is empty; the source-fallback cascade result is unchanged when
  render is off.
- Sidecar unit (`render/tests/`): the expand loop against a local fixture page with a fake
  "read more" control that reveals more text on click - no live DataDome in CI.
- Live validation (manual, post-deploy): reprocess the inc.com URL and confirm the word
  count roughly doubles. DataDome cannot run in CI, so this stays a human check.

## Scope boundaries (YAGNI)

Not in this build: proxy support, per-host click selectors, CAPTCHA solving, a persistent
browser pool, a PDF render path. The Camoufox engine is swappable behind the sidecar's HTTP
API if DataDome adapts.

## Version

0.38.0 (app + tts-wrapper + new render image), shipped as one release.
