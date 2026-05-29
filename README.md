# Audicle

Self-hosted Podcasting 2.0 service that turns saved articles into a personal podcast feed.

Paste a URL, wait a few minutes, get an episode with cloned-voice narration, artwork, and a WebVTT transcript. Subscribe in Pocket Casts, Overcast, or Apple Podcasts the same way you'd subscribe to anyone else's show.

Tagline: *your reading list, as a podcast you own.*

## Why

I read too much, drive too much, and the existing "article to audio" tools either lock the audio behind their app, charge per minute, or use voices that sound like the airport PA. I wanted something that:

- runs on my own GPU box
- produces a real podcast feed any podcatcher can subscribe to
- uses my own voice (or any voice I have rights to)
- keeps the source URL list private

That's what this is. If you don't have a GPU, it'll run on CPU too, just 5-10x slower per chunk.

## What's in the repo

```
backend/        FastAPI app, SQLite, the job pipeline
tts-wrapper/    XTTS-v2 model server (separate container for GPU isolation)
frontend/       React + Tailwind operator UI
data/           runtime artifacts (gitignored: SQLite, MP3, JPG, VTT)
docker-compose.yml
build-plan.md   the design document the implementation tracks against
```

## Quickstart

You need Docker, docker-compose, a `voice.wav` file (3-60s of clean speech), and an LLM key.

```bash
git clone https://github.com/ttlequals0/Audicle && cd Audicle
cp .env.example .env
# edit .env, see "Required env vars" below
cp /path/to/your/voice.wav backend/app/reference/voice.wav
docker compose up -d
```

The web UI is at `http://localhost:8000/`. The RSS feed is `http://localhost:8000/rss/rss.xml` -- paste that into any podcatcher.

If you don't have a CUDA GPU, override the wrapper to use CPU:

```bash
TTS_DEVICE=cpu docker compose up -d
```

First-run model download is ~2 GB and lives in a named volume (`hf_cache`).

## Required env vars

| Variable | What it is | Example |
|---|---|---|
| `BASE_URL` | Public-facing URL for the feed and media | `https://podcast.example.com` |
| `FEED_TITLE` | Podcast title | `Drew's reading list` |
| `FEED_AUTHOR` | Author / itunes:author | `Drew K.` |
| `FEED_EMAIL` | Owner email (required by Apple) | `you@example.com` |
| `FEED_CATEGORY` | iTunes category (see list below) | `Technology` |
| `FEED_LANGUAGE` | RFC 5646 tag | `en-US` |
| `FIRECRAWL_URL` | Self-hosted Firecrawl base URL | `http://firecrawl:3002` |
| `LLM_PROVIDER` | `openai-compatible` or `anthropic` | `openai-compatible` |
| `OPENAI_BASE_URL` | for openai-compatible only | `http://llm:8080/v1` |
| `OPENAI_API_KEY` | for openai-compatible | `sk-...` |
| `ANTHROPIC_API_KEY` | for anthropic | `sk-ant-...` |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Admin UI auth (if `AUTH_ENABLED=true`) | -- |
| `SESSION_SECRET_KEY` | 32+ random bytes for session signing | `openssl rand -hex 32` |

Full list with defaults lives in `.env.example`. The runtime allowlist (what's editable from the UI without a restart) is enforced in `backend/app/services/runtime_settings.py`.

## Valid iTunes categories

Apple's parser rejects anything not on its list. The current ones (from Apple's RSS spec, May 2026):

```
Arts, Business, Comedy, Education, Fiction, Government, History,
Health & Fitness, Kids & Family, Leisure, Music, News, Religion & Spirituality,
Science, Society & Culture, Sports, Technology, True Crime, TV & Film
```

Subcategories aren't currently surfaced in the UI -- set the top-level category and call it done. If Apple Podcasts shows your feed as "Unknown" after submission, it's almost always a category typo.

## Reference voice

The wrapper conditions on a single short clip you supply. Recommended spec is mono, 24 kHz, 8-12 seconds, around 250 kB to 1 MB. Hard limits enforced by `POST /api/v1/reference/commit` are 3-60 s and <= 5 MB. See `backend/app/reference/README.md` for the sourcing playbook (record yourself, reuse a creative-commons clip, or synthesize one).

Two notes the docs page doesn't repeat:

- The audio quality of the output is mostly determined by the quality of this clip. Cleaning up the source clip (noise reduction, leveling) buys you more than tweaking the TTS knobs.
- The Settings page in the UI lets you upload a candidate, audition it via `POST /api/v1/reference/test`, and commit only if you like it. Skip the audition at your peril.

## Licensing notes

The application code is MIT. Two things downstream of it are not:

- **XTTS-v2 weights** ship under the Coqui Public Model Licence 1.0.0 (CPML). It is non-commercial. Personal self-hosted use is fine; selling the generated audio isn't. If you need a commercial pipeline, swap the wrapper for a different TTS model that's licensed for it. The wrapper interface (`POST /generate` + `POST /reload`) is small enough that this is straightforward.
- **Coqui TTS on Python 3.13**: the upstream `coqui-tts` PyPI package as of 2026-05 declares `python_requires<3.13`. The wrapper Dockerfile pins Python 3.11 for that reason. If you try to run the wrapper on a 3.13 host outside the container, install from the `idiap/coqui-ai-TTS` fork instead -- that one has the version constraint lifted.

The Audicle name and logo are reserved; see `branding/README.md`.

## Architecture

```
URL --> extract (Firecrawl) --> cleanup (LLM) --> corrections (regex)
                                                       |
                                                       v
                                              chunk + TTS (XTTS-v2)
                                                       |
                                                       v
                                       audio (ffmpeg) + artwork + VTT
                                                       |
                                                       v
                                          finalize (write DB + RSS)
```

Two containers: the backend (FastAPI + SQLite) and the TTS wrapper (separate so GPU memory stays isolated and the model only reloads when the voice changes). They share a `/data` volume so the backend can read what the wrapper produces.

There's no message queue. SQLite handles the work queue via a single locked row update -- fine for one or two operators, not the right shape if you ever want to fan out across hosts.

## Operating

- `/health/live` is a flat liveness probe.
- `/health/ready` aggregates DB, ffmpeg, TTS wrapper, Firecrawl, and (optionally) LLM probes. Returns 503 if any check fails.
- `POST /api/v1/purge` removes episodes older than the retention window. Confirmation required.
- Background retention sweep runs from the worker on a fixed cadence -- configurable via `RETENTION_DAYS`.
- Default rate limits are conservative; the `slowapi` middleware wiring lives in `backend/app/main.py`.

If something's broken, start with `/health/ready` -- it tells you which dependency is unhappy. Logs live where docker put them (`docker compose logs app` / `docker compose logs tts-wrapper`).

## Development

Backend:

```bash
uv sync
uv run pytest                              # 332 tests, ~30s
uv run uvicorn app.main:create_app --factory --reload --app-dir backend
```

Frontend:

```bash
cd frontend && npm install && npm run dev   # Vite, hot reload
```

There's an OpenAPI dump at `openapi.yaml`; regenerate via `uv run python scripts/dump_openapi.py`.

CodeQL runs on every PR. Pre-commit hooks aren't installed by default -- wire them with `git config core.hooksPath .githooks` after the `.githooks` directory is in place.

## Status

This is the working main branch through Phase 13 of the build plan. The feed serves real episodes end to end. Auth, retention, runtime settings, PWA install, reference-voice management, and the operator UI are all wired.

What's not done: multi-host scale-out, multi-user accounts, and any monetization plumbing. None of those are on the roadmap.
