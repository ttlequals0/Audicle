# Installation

You need Docker and docker-compose. The app boots unconfigured: set the LLM provider and model, feed metadata, and admin password, and upload a reference voice, from the Settings UI after it starts. No env editing or `voice.wav` required up front.

```bash
git clone https://github.com/ttlequals0/Audicle && cd Audicle
cp .env.example .env   # compose requires .env to exist; pre-set BASE_URL and any defaults here
docker compose up -d
```

The web UI is at `http://localhost:8000/`. It is an installable PWA: add it to a phone home screen and it behaves like an app, with updates applied automatically.

The RSS feed is served at a slug derived from the feed name, so `FEED_TITLE="Articles of Interest"` becomes `/rss/articles_of_interest.xml`. The Feed page shows the exact URL with a copy button; paste it into any podcatcher. Renaming the feed changes the slug and mints new feed and episode GUIDs, so subscribers resubscribe to the new URL.

## Three containers

| Service | Image | What it does |
|---|---|---|
| `app` | `ttlequals0/audicle` | FastAPI API, the web UI, the RSS feed, and the job worker |
| `tts-wrapper` | `ttlequals0/audicle-tts` | The Chatterbox TTS server, GPU-pinned by default |
| `render` | `ttlequals0/audicle-render` | Headful-browser sidecar for expand-gated pages; optional, the app tolerates it being down |

The backend and wrapper share a `/data` volume so the backend can read the audio the wrapper produces. The wrapper is a separate container so GPU memory stays isolated and the model reloads only when the voice changes.

## File permissions

The containers run as a non-root user (uid 1000). If you bind-mount host directories (or set `user:` in compose), make them writable by uid 1000 so the app can write the database and media and seed the default prompt and corrections:

```bash
chown -R 1000:1000 ./data ./backend/app/prompts ./backend/app/corrections ./backend/app/reference
```

## First-run model download

The first run downloads about 2 GB of model weights, which persist on the `./data` volume under `hf_cache/` and `tts_home/` (the wrapper sets `HF_HOME`/`TTS_HOME` there), so restarts load from disk instantly.

## CPU-only deployment

No CUDA GPU? `TTS_DEVICE=cpu` alone is not enough: the stock compose file pins the CUDA image and reserves an NVIDIA device. Three steps get you a CPU deployment (5 to 10 times slower):

1. Use the published CPU image `ttlequals0/audicle-tts:<version>-cpu` (available for every release from 0.56.0 on), or build it yourself from the CPU Dockerfile:

   ```bash
   docker build -t audicle-tts:cpu -f tts-wrapper/Dockerfile.cpu tts-wrapper/
   ```

2. Point the `tts-wrapper` service at that image and remove the `deploy.resources` GPU reservation from it.
3. Set `TTS_DEVICE=cpu` in `.env`.

Then `docker compose up -d` as usual.

## Required configuration

Nothing is strictly required to boot, but a working feed needs the variables below, set in `.env` or at runtime in Settings. See [Environment variables](environment-variables.md) for the full list.

| Variable | What it is | Example |
|---|---|---|
| `BASE_URL` | Public-facing URL for the feed and media | `https://podcast.example.com` |
| `FEED_TITLE` | Podcast title | `Drew's reading list` |
| `FEED_AUTHOR` | Author / itunes:author | `Drew K.` |
| `FEED_EMAIL` | Owner email (required by Apple) | `you@example.com` |
| `FEED_CATEGORY` | iTunes category ([valid list](feeds-and-podcasting.md#valid-itunes-categories)) | `Technology` |
| `FEED_LANGUAGE` | RFC 5646 tag | `en-US` |
| `LLM_PROVIDER` | One of the four [providers](llm-providers.md) | `openai-compatible` |

The admin password lives under Settings > security (a bcrypt hash in the DB); until it is set the app runs in open convenience mode, and warns you about it when the server looks internet-facing.

## Extraction out of the box

Extraction works with no extra services: the default `direct` engine fetches the page in-process and parses it with trafilatura. To use a self-hosted [Firecrawl](https://github.com/firecrawl/firecrawl) instead, set `EXTRACTION_ENGINE=firecrawl` and point `FIRECRAWL_URL` at it. Either way, JS-rendered and bot-gated pages fall back through the cascade described in [Paywalled articles](paywalls.md).

[< Docs index](README.md)
