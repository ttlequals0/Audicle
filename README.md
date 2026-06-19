<p align="center">
  <img src="branding/wordmark.svg" alt="Audicle" width="320">
</p>

# Audicle

Self-hosted Podcasting 2.0 service that turns saved articles into a personal podcast feed.

Paste a URL or upload a document (PDF, DOCX, Markdown, text, or HTML), wait a few minutes, get an episode with cloned-voice narration, artwork, and a WebVTT transcript. Subscribe in Pocket Casts, Overcast, or Apple Podcasts the same way you'd subscribe to anyone else's show.

Tagline: *your reading list, as a podcast you own.*

## Screenshots

Home -- paste a URL or upload a file, and it joins the feed.

<p align="center">
  <img src="docs/screenshot-home-desktop.png" alt="Home, desktop" width="600">
  <img src="docs/screenshot-home-mobile.png" alt="Home, mobile" width="190">
</p>

Feed -- your episodes with inline players, transcripts, and per-episode actions.

<p align="center">
  <img src="docs/screenshot-feed-desktop.png" alt="Feed, desktop" width="600">
  <img src="docs/screenshot-feed-mobile.png" alt="Feed, mobile" width="190">
</p>

Settings -- provider, voices, prompts, and pronunciation corrections.

<p align="center">
  <img src="docs/screenshot-settings-desktop.png" alt="Settings, desktop" width="600">
  <img src="docs/screenshot-settings-mobile.png" alt="Settings, mobile" width="190">
</p>

## Sample

A 30-second clip of cloned-voice narration (a news article).

https://github.com/user-attachments/assets/4e4e9f05-9da7-4f27-b7e8-41b9dfe1bee3

[Download the MP3](docs/sample.mp3)

## Why

I read too much, I like having my hands free on the go, and the existing "article to audio" tools either lock the audio behind their app, charge per minute, or use voices that sound like airport PA systems. I wanted something that:

- I have full control over
- produces a real podcast feed any podcatcher can subscribe to
- uses my own voice (or any voice I have rights to)
- keeps the source URL list private

That's what this is. If you don't have a GPU, it'll run on CPU too, just slower.

## What's in the repo

```
backend/        FastAPI app, SQLite, the job pipeline
tts-wrapper/    TTS model server (Chatterbox; separate GPU container)
render/         full-article render sidecar (Camoufox + xvfb; clicks expand gates)
frontend/       React + Tailwind operator UI
data/           runtime artifacts (gitignored: SQLite, MP3, JPG, VTT)
docker-compose.yml
build-plan.md   the design document the implementation tracks against
```

## Quickstart

You need Docker and docker-compose. The app boots unconfigured -- set the LLM
provider/model, feed metadata, admin password, and upload a reference voice from
the Settings UI after it starts (no env or `voice.wav` required up front).

```bash
git clone https://github.com/ttlequals0/Audicle && cd Audicle
cp .env.example .env   # optional: pre-set BASE_URL and any defaults
docker compose up -d
```

The web UI is at `http://localhost:8000/`. The RSS feed lives at a slug derived
from your feed name -- e.g. `FEED_TITLE="Articles of Interest"` is served at
`http://localhost:8000/rss/articles_of_interest.xml`. The Feed page in the UI
shows the exact URL with a copy button; paste it into any podcatcher. Renaming
the feed changes the slug and mints new feed/episode GUIDs, so subscribers
resubscribe to the new URL.

The container runs as a non-root user (uid 1000). If you bind-mount host
directories (or set `user:` in compose), make them writable by uid 1000 so the
app can write the database, media, and seed the default prompt/corrections:

```bash
chown -R 1000:1000 ./data ./backend/app/prompts ./backend/app/corrections ./backend/app/reference
```

If you don't have a CUDA GPU, override the wrapper to use CPU:

```bash
TTS_DEVICE=cpu docker compose up -d
```

First-run model download is ~2 GB and persists on the `./data` volume under
`hf_cache/` and `tts_home/` (the wrapper sets `HF_HOME`/`TTS_HOME` there), so
restarts load from disk instantly.

Article extraction works out of the box with no extra service: the default
`direct` engine fetches the page in-process and parses it with trafilatura. To
use a self-hosted [Firecrawl](https://github.com/firecrawl/firecrawl) instead,
set `EXTRACTION_ENGINE=firecrawl` and point `FIRECRAWL_URL` at it. Either way,
JS-rendered and bot-gated pages still fall back to FlareSolverr and the web
archive (see [Paywalled articles](#paywalled-articles)).

## Required env vars

| Variable | What it is | Example |
|---|---|---|
| `BASE_URL` | Public-facing URL for the feed and media | `https://podcast.example.com` |
| `FEED_TITLE` | Podcast title | `Drew's reading list` |
| `FEED_AUTHOR` | Author / itunes:author | `Drew K.` |
| `FEED_EMAIL` | Owner email (required by Apple) | `you@example.com` |
| `FEED_CATEGORY` | iTunes category (see list below) | `Technology` |
| `FEED_LANGUAGE` | RFC 5646 tag | `en-US` |
| `EXTRACTION_ENGINE` | `direct` (built-in, no extra service) or `firecrawl` | `direct` |
| `FIRECRAWL_URL` | Self-hosted Firecrawl base URL (only when `EXTRACTION_ENGINE=firecrawl`) | `http://firecrawl:3002` |
| `FIRECRAWL_API_KEY` | Optional bearer token for a Firecrawl behind auth (blank = open) | _(unset)_ |
| `LLM_PROVIDER` | `openai-compatible`, `anthropic`, `openrouter`, or `ollama` | `openai-compatible` |
| `OPENAI_BASE_URL` | for openai-compatible | `http://llm:8080/v1` |
| `OPENAI_API_KEY` | for openai-compatible | `sk-...` |
| `ANTHROPIC_API_KEY` | for anthropic | `sk-ant-...` |
| `OPENROUTER_API_KEY` | for openrouter (base URL is fixed) | `sk-or-...` |
| `OLLAMA_BASE_URL` | for ollama | `http://host.docker.internal:11434/v1` |
| `SESSION_SECRET_KEY` | Optional session-signing key; auto-generated and persisted to the DB when blank | `openssl rand -hex 32` |
| `SESSION_COOKIE_SECURE` | Require HTTPS for the session cookie; true by default | `false` for localhost dev |
| `TRUST_PROXY_HEADERS` | Key the login rate-limit and IP lockout off `X-Forwarded-For` instead of the socket peer; enable only behind a trusted proxy | `true` behind Cloudflare/nginx |
| `TRUSTED_PROXY_HOPS` | How many proxy hops to trust (entries counted from the right of `X-Forwarded-For`) | `1` |

Nothing above is required: the app boots unconfigured and you set operational config (LLM provider/model, feed metadata, connection URLs) and the admin password at runtime in the Settings UI. The admin password is set under Settings > Security (bcrypt hash stored in the DB); until then the app runs in open convenience mode. Full list with defaults lives in `.env.example`. The runtime allowlist (what's editable from the UI without a restart) is enforced in `backend/app/services/runtime_settings.py`.

## Valid iTunes categories

Apple's parser rejects anything not on its list. The current ones (from Apple's RSS spec, May 2026):

```
Arts, Business, Comedy, Education, Fiction, Government, History,
Health & Fitness, Kids & Family, Leisure, Music, News, Religion & Spirituality,
Science, Society & Culture, Sports, Technology, True Crime, TV & Film
```

Subcategories aren't currently surfaced in the UI -- set the top-level category and call it done. If Apple Podcasts shows your feed as "Unknown" after submission, it's almost always a category typo.

## Voices

The wrapper narrates each episode by conditioning on a short reference clip you supply. You manage clips in Settings under "voices": a Default fallback plus five labelled slots. Every row plays back its stored clip and can audition a TTS sample, so you hear a voice before you use it.

Each episode picks a voice when you submit it -- Random (a random filled slot), Last used, or a specific slot -- from a collapsed picker under the Submit button on the Home screen. With no slots filled, every episode uses the Default voice.

Recommended clip: mono, 24 kHz, 8-12 seconds, around 250 kB to 1 MB. The hard limits enforced on upload are 3-60 seconds and 5 MB. You can upload WAV, MP3, M4A/AAC, FLAC, or OGG/Opus; anything that isn't already a WAV is converted to one with ffmpeg before it's stored. See `backend/app/reference/README.md` for the sourcing playbook (record yourself, reuse a creative-commons clip, or synthesize one).

The output quality is mostly set by the clip quality. Cleaning up the source -- noise reduction, leveling -- helps more than any TTS knob.

## End-of-episode chime

Settings has an "end chime" section where you upload one short clip that plays at the end of every episode -- handy for telling back-to-back episodes apart on autoplay. Turn it on with the `CHIME_ENABLED` toggle under TTS settings; the clip is transcoded and loudness-matched to the narration. Upload WAV/MP3/M4A/FLAC/OGG, trimmed to about 15 seconds. Delete it to stop.

## Episode artwork

Each episode's cover goes into the feed (`itunes:image`) and is also embedded into the MP3, because some players -- Pocket Casts among them -- only read embedded art and ignore the feed tag. Episodes that didn't pull their own cover fall back to the show's feed image. The embedded copy is a smaller 1400px JPEG (`EMBED_ARTWORK_SIZE_PX`) to hold file size down; the feed still serves the full 3000px master.

## Pronunciation corrections

Settings has a corrections table for words the narrator mispronounces. Each row is a match term, the spoken form to say instead, a mode, an optional IPA field, and an "Aa" case toggle. A curated seed set ships built in (`GET /api/v1/corrections/seed`); your rows override it.

- spoken is what drives narration -- write it the way you want it read ("four oh four media", "clawed").
- mode is override (say the spoken form), word (read an acronym as a word), or spell (read it letter by letter).
- Aa makes the match case-sensitive; off (the default) folds case, so a "404 media" row also catches "404 Media".
- ipa is optional and only feeds the PLS lexicon export -- it does NOT affect narration. Audicle auto-derives it from the spoken form, so it can look like an unreadable string of phonetic symbols, and it can go stale if you edit the spoken text later. That is expected; clear it or ignore it unless you use the PLS export.

## Webhooks

Audicle can POST a JSON payload to a URL of yours every time an episode finishes
(`episode.processed`) or fails (`episode.failed`) -- handy for a Slack/Discord ping, a
dashboard, or kicking off something downstream. Set `WEBHOOK_URL` in Settings (the
"webhooks" section); leave it blank to turn it off. The "send test webhook" button there
fires a sample payload at the saved URL and shows the response, so you can wire up a
receiver before running a real article.

Payload fields:

| Field | Type | When | Meaning |
|---|---|---|---|
| `event` | string | always | `episode.processed` or `episode.failed` |
| `episode_id` | string | always | the episode's stable id |
| `title` | string | always | episode title (falls back to the filename or URL) |
| `voice` | string | always | the reference voice that narrated it -- a slot label, `Slot N`, or `Default` |
| `source_type` | string | always | `url` or `upload` |
| `url` | string | url jobs | the source article URL |
| `source_filename` | string | upload jobs | the uploaded document's name |
| `reprocess` | bool | always | true if this run was a reprocess, not a first pass |
| `time_to_process_secs` | number or null | processed | seconds from claim to finish (null for very old jobs) |
| `time_to_process` | string or null | processed | the same time as `mm:ss` |
| `length` | string or null | processed | the episode's audio length as `mm:ss` |
| `error` | string | failed | the failure message |
| `stage` | string | failed | the pipeline stage that failed (e.g. `tts`, `extract`) |

A finished URL episode:

```json
{
  "event": "episode.processed",
  "episode_id": "a1b2c3d4e5f6",
  "title": "An Interesting Article",
  "voice": "Morgan",
  "source_type": "url",
  "url": "https://example.com/article",
  "reprocess": false,
  "time_to_process_secs": 246.0,
  "time_to_process": "04:06",
  "length": "12:30"
}
```

A failed job:

```json
{
  "event": "episode.failed",
  "episode_id": "a1b2c3d4e5f6",
  "title": "https://example.com/article",
  "voice": "Default",
  "source_type": "url",
  "url": "https://example.com/article",
  "reprocess": false,
  "error": "TTS unreachable",
  "stage": "tts"
}
```

An upload episode is the same shape but with `"source_type": "upload"` and a
`"source_filename"` instead of `url`. The test button's payload adds a `"test": true`
flag so your receiver can tell it apart from a real run.

Delivery is fire-and-forget: it runs as a background task with a short timeout
(`WEBHOOK_TIMEOUT_SECONDS`, default 10s) and a few retries with backoff, so a dead or slow
receiver never delays or fails the episode. To test from the API instead of the UI,
`POST /api/v1/webhooks/test` returns `{ "delivered", "status_code", "error" }`.

A failed job can also be requeued straight from the Recents list on the Home screen -- URL jobs re-fetch, uploads re-run from the stored original.

## Paywalled articles

Some sites hand a scraper only a teaser and hide the rest behind a paywall. That teaser is long enough to look like a real article but turns into a 25-second junk episode. The "article proxy / paywall sites" section in Settings lets you route those hosts through a bypass strategy.

You pick a default strategy and a teaser threshold, then list per-site overrides. The default strategy applies to any host: when a scrape comes back near-empty (below `MIN_EXTRACTION_CHARS`, i.e. a hard block that returned almost nothing), Audicle retries through the default before giving up. Per-site rules are overrides -- a listed host uses its own strategy and its own (higher) teaser threshold, so a partial teaser that clears the global floor still triggers a retry; a host set to `none` opts out. If the retry still comes back short, the job fails cleanly instead of narrating the stub. A legitimately short article (above the floor) is left alone. Same thing behind `GET`/`PUT /api/v1/source-fallbacks`.

The strategies:

- `googlebot` (the default): re-fetch the same URL with a Googlebot user agent and a crawler `X-Forwarded-For`. SEO-metered paywalls serve the full article to the crawler, so this is the one that works most often. It runs through the scrape headers, not a separate proxy container.
- `freedium`: rewrite the URL to a Freedium reader proxy. Best for Medium.
- `custom`: rewrite to your own reader-proxy template, any URL containing `{url}`.
- `flaresolverr`: fetch the page through your FlareSolverr (a real browser) instead of Firecrawl. For hosts that hard-block the scraper's datacenter IP with a 403 (e.g. the NYT), where the Googlebot header trick can't help -- the solver runs Chrome from a residential IP and the publisher serves the article normally. Needs `FLARESOLVERR_URL` set. Since 0.26.0 Audicle does this automatically on any hard block (see below), so this per-host setting is now mainly an explicit override -- e.g. to force the solver for a host that returns a teaser rather than a near-empty page. A `flaresolverr` rule can also carry a cookie jar (below) for sites you subscribe to.
- `archive`: pull a saved copy of the article from a public archive. Tries the [Wayback Machine](https://web.archive.org) first (a clean API, no bot wall, no cookies), then archive.today through FlareSolverr. Useful when a metered or soft wall, or an older article, was archived while it was still free. It is not a way past a hard subscriber wall: if no free copy was ever archived, there is nothing to fetch.
- `none`: don't try anything. A matched host that comes back short just fails, which is what you want for a hard paywall you'd rather skip than narrate.

A built-in Medium-to-Freedium rule ships on by default. Your own rules layer on top and win when they collide on a host. The whole thing is gated by `EXTRACTION_FALLBACKS_ENABLED`; set it false to always use the direct scrape (no default-proxy retry either).

Some sites pad a one-paragraph teaser with a "Recommended For You" rail and a "Latest News" list, so the scraped text clears the teaser threshold on chrome alone and the bypass never fires. For a host with a rule, Audicle reads the page's own JSON-LD `articleBody` length and judges the teaser by that, so the lede is caught and routed to the bypass. Use the "test a URL" button in the paywall settings to run your rules against one link and see how many characters come back and which strategy matched -- the quickest way to confirm a cookie jar still works.

Hard blocks are handled automatically, not as a per-host strategy. If you've set `FLARESOLVERR_URL` (env or live in Settings), Audicle re-fetches through your own [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) -- a real browser from a residential IP -- and pulls the article out of the solved HTML. It fires for any host in two cases: a scrape that looks like a Cloudflare challenge ("Just a moment...", a Ray ID), or a near-empty scrape (below `MIN_EXTRACTION_CHARS`) -- the signature of a 403/IP block where the site served almost nothing. It stays bounded: a real article or a partial teaser never triggers a browser solve. Audicle doesn't bundle a solver. As a final automatic step, when a hard block isn't recovered any other way, Audicle tries a Wayback capture before failing (`ARCHIVE_FALLBACK_ENABLED`, on by default).

Some sites hide the second half of an article behind an "EXPAND TO CONTINUE READING" click (inc.com and others behind DataDome). FlareSolverr clears the challenge but runs a headless browser that can't click, so it only returns the visible front half. The bundled `audicle-render` sidecar handles this: it loads the page in a headful Camoufox browser, clicks the expander until the body stops growing, and hands back the full HTML. Give a site the `render` strategy in Site overrides (inc.com ships with it; the shipped defaults live in `config.RENDER_BUILTIN_HOSTS`) -- render then runs after the cascade as enrichment when FlareSolverr got a partial, and as a rescue when the cascade was blocked entirely, so a render site always gets the stronger browser. A page that still looks truncated triggers it even without a rule. Set `RENDER_URL` (empty disables it); the sidecar is internal-only. DataDome is probabilistic, so a render that hits a CAPTCHA falls back to the front-half partial and logs it.

When extraction still fails, the job says why: a hard block with no solver set points you at `FLARESOLVERR_URL`; a hard block the solver couldn't clear means the site likely needs a login; a short teaser means add a per-host bypass.

### Subscriber paywalls (cookie jar)

Some walls never serve the body to a logged-out request, no matter the IP -- a hard subscriber paywall like Crain's / Chicago Business hands every anonymous reader the same teaser, so even a fresh FlareSolverr session gets nothing more. If you pay for the site, point the host at the `flaresolverr` strategy and paste your own logged-in session cookies into its cookie jar (`name=value; name2=value2`, as you'd copy them from your browser). The solver then fetches the article as you and pulls the full text.

A session cookie is full account access, so use a dedicated login where the site allows one, and treat the jar like a password. Audicle holds it with the other secrets, never writes it to a log, and reads it back masked once saved -- re-saving the masked value keeps the stored cookies, clearing the field removes them. Needs `FLARESOLVERR_URL` set.

## Licensing notes

The application code is MIT. A few things downstream of it have their own terms:

- **Chatterbox** is the TTS engine. The `chatterbox-tts` library and its model weights are MIT, so there is no non-commercial restriction on the model itself. Every output does carry Resemble's inaudible PerTh watermark for provenance, and there is no flag to turn it off.
- **Wrapper Python pin**: the wrapper Dockerfile pins Python 3.11, since `chatterbox-tts` caps `numpy<2` below Python 3.13. The backend is separate: it requires Python `>=3.13` and ships on a `python:3.14-slim` image.

The Audicle name and logo are reserved; see `branding/README.md`.

## Architecture

```
        paywall bypass: a matched host's teaser triggers a re-scrape via
        Googlebot / Freedium / a custom proxy (or a clean fail);
        a detected Cloudflare challenge auto-routes through FlareSolverr
        |
        v
URL --> extract (direct / Firecrawl) --> cleanup (LLM) --> corrections (regex)
                                                       |
                                                       v
                                              chunk + TTS (Chatterbox)
                                                       |
                                                       v
                                   quality gate: audio QA + optional
                                   Whisper ASR verify --> regen on fail
                                                       |
                                                       v
                                       audio (ffmpeg) + artwork + VTT
                                                       |
                                                       v
                                          finalize (write DB + RSS)
```

The paywall bypass is operator-configured -- see "Paywalled articles" above.

The chunker self-heals before TTS: it splits run-on sentences that arrive glued together (`end.Next`) and, when a long sentence has no comma or semicolon to break on, falls back to a whitespace split instead of failing the job. Only a single word longer than the character cap is treated as unsplittable.

### TTS verification

Every chunk passes a quality gate before it reaches the audio stage. The signal-level audio analysis catches a take that came back as a flat drone, steady noise, or a repetition; a bad take is regenerated with a fresh seed (Chatterbox is non-deterministic, so a re-gen usually recovers).

You can add a second, optional check: ASR verification. When it is on, the GPU wrapper transcribes each produced chunk with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) and the backend compares that transcript to the text it asked the wrapper to speak. A high word-level divergence means the audio does not say what it should -- dropped content, a hallucinated run, or a leaked preamble -- and the chunk is regenerated on the same budget. The transcription is blind (the expected text is never fed to Whisper as a prompt), so the comparison stays honest.

It is off by default and adds latency per chunk, so it takes two switches: `WHISPER_ENABLED=true` on the wrapper (loads the model) and `WHISPER_VERIFY_ENABLED=true` on the backend (requests and acts on the transcript). The backend half -- enable, divergence threshold, and minimum words -- is operator-tunable live from the Settings page (or `PUT /api/v1/settings`), so you can flip the gate on and adjust strictness without a restart; the wrapper's `WHISPER_ENABLED` stays an env switch since it loads the model at startup. Tune `WHISPER_MODEL` for the accuracy/speed trade. See `.env.example` for the full set.

Two containers: the backend (FastAPI + SQLite) and the TTS wrapper (separate so GPU memory stays isolated and the model only reloads when the voice changes). They share a `/data` volume so the backend can read what the wrapper produces.

There's no message queue. SQLite handles the work queue via a single locked row update -- fine for one or two operators, not the right shape if you ever want to fan out across hosts.

## Operating

- `/health/live` is a flat liveness probe.
- `/health/ready` aggregates DB, ffmpeg, TTS wrapper, Firecrawl, and (optionally) LLM probes. Returns 503 if any check fails.
- `POST /api/v1/purge` removes episodes older than the retention window. Confirmation required.
- Background retention sweep runs from the worker on a fixed cadence -- configurable via `RETENTION_DAYS`.
- Default rate limits are conservative; the `slowapi` middleware wiring lives in `backend/app/main.py`.

If something's broken, start with `/health/ready` -- it tells you which dependency is unhappy. Logs live where docker put them (`docker compose logs app` / `docker compose logs tts-wrapper` / `docker compose logs render`).

## Development

Backend:

```bash
uv sync
uv run pytest                              # 729 tests, ~60s
uv run uvicorn app.main:create_app --factory --reload --app-dir backend
```

Frontend:

```bash
cd frontend && npm install && npm run dev   # Vite, hot reload
```

There's an OpenAPI dump at `openapi.yaml`; regenerate via `uv run python scripts/dump_openapi.py`.

CodeQL runs on every PR. Pre-commit hooks aren't installed by default -- wire them with `git config core.hooksPath .githooks` after the `.githooks` directory is in place.

## LLM Disclosure

This project was developed using AI agents as a pair programmer. It was NOT vibe coded. For context, I'm a systems engineer who also writes code professionally with 15+ years of experience. The codebase follows engineering best practices, and all architecture and design decisions were made by me, not by AI. All code generated by LLMs was reviewed and tested by me, a human.

## Credits

The paywall bypass strategies are inspired by [Ladder](https://github.com/everywall/ladder). Audicle doesn't run Ladder or depend on it; the Googlebot fetch is reimplemented natively here as scrape headers. Credit to that project for the technique.
