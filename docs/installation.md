# Installation

You need Docker and docker-compose. The app boots unconfigured: set the LLM provider and model, feed metadata, and admin password, and upload a reference voice, from the Settings UI after it starts. Only the image release tag is required in `.env`; application settings and voices are configured after startup.

```bash
git clone https://github.com/ttlequals0/Audicle && cd Audicle
cp .env.example .env
# Set BUILD_VERSION in .env to the release tag you want to run.
docker compose up -d
```

The web UI is at `http://localhost:8000/`. It is an installable PWA: add it to a phone home screen and it behaves like an app, with updates applied automatically.

The RSS feed is served at a slug derived from the feed name, so `FEED_TITLE="Articles of Interest"` becomes `/rss/articles_of_interest.xml`. The Feed page shows the exact URL with a copy button; paste it into any podcatcher. Renaming the feed changes the slug and mints new feed and episode GUIDs, so subscribers resubscribe to the new URL.

## Services

When upgrading from a TTS image that ran as root, stop the stack and run `sudo chown -R 1000:1000 ./data` from the Compose directory before starting the new image. Existing bind-mounted model caches keep their old ownership; the image cannot change it. Substitute the host data path if you use a different mount.

| Service | Image | What it does |
|---|---|---|
| `app` | `ttlequals0/audicle` | FastAPI API, the web UI, the RSS feed, and the job worker |
| `tts-wrapper` | `ttlequals0/audicle-tts` | The Chatterbox TTS server, GPU-pinned by default |
| `render` | `ttlequals0/audicle-render` | Headful-browser sidecar for expand-gated pages; optional, the app tolerates it being down |
| `render-egress` | `ttlequals0/audicle-render-egress` | Egress proxy that blocks renderer access to private and special-use networks |

The backend and wrapper share a `/data` volume so the backend can read the audio the wrapper produces. The wrapper is a separate container so GPU memory stays isolated and the model reloads only when the voice changes.

Size the `./data` volume for the TTS chunk cache too: it keeps roughly 170 MB per hour of narrated audio under `data/tts_cache`, for `TTS_CACHE_RETENTION_DAYS` (default 7) days.

## File permissions

The app and TTS wrapper run as uid 1000. The renderer starts with limited privileges to configure its firewall, then drops to uid 1000. If you bind-mount host directories (or set `user:` in compose), make them writable by uid 1000 so the app can write the database and media and seed the default prompt and corrections:

```bash
chown -R 1000:1000 ./data ./backend/app/prompts ./backend/app/corrections ./backend/app/reference
```

## First-run model download

The first run downloads about 2 GB of model weights, which persist on the `./data` volume under `hf_cache/` and `tts_home/` (the wrapper sets `HF_HOME`/`TTS_HOME` there), so restarts load from disk instantly.

## CPU-only deployment

No CUDA GPU? `TTS_DEVICE=cpu` alone is not enough: the stock compose file pins the CUDA image and reserves an NVIDIA device. The `docker-compose.cpu.yml` override swaps in the CPU image (`ttlequals0/audicle-tts:<version>-cpu`, published for every release from 0.56.0 on), sets `TTS_DEVICE=cpu` and drops the GPU reservation. Expect generation to be 5 to 10 times slower.

```bash
docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d
```

The override needs Docker Compose 2.24 or newer. To build the CPU image yourself instead of pulling it:

```bash
docker build --build-arg WRAPPER_VERSION=$(cat VERSION) -t ttlequals0/audicle-tts:<version>-cpu -f tts-wrapper/Dockerfile.cpu tts-wrapper/
```

### Apple Silicon

From 0.62.0 the app, render, render-egress and TTS `-cpu` images are published for both linux/amd64 and linux/arm64. Docker Desktop on an M-series Mac pulls the arm64 variants and runs them natively; use the CPU override above. The GPU wrapper image (`ttlequals0/audicle-tts:<version>`) stays amd64-only, so do not use it on a Mac.

Give Docker Desktop enough memory for the TTS wrapper. It starts trimming memory at `TTS_MEMORY_SOFT_LIMIT_MB` (default 8000) and restarts itself at `TTS_MEMORY_HARD_LIMIT_MB` (default 14000). The VM needs to hold at least the soft limit plus the app and render containers. Raise the VM memory in Docker Desktop under Settings > Resources, or lower both limits in `.env`.

The render sidecar is optional. If you leave it out or it is down, the app fails closed to the normal extraction cascade.

## Required configuration

Set the public feed URL, an LLM connection and model, and a reference voice in Settings. Use Services > Connections for the public URL, Services > LLM for the AI provider, Voice > Voices for a reference clip, and Publishing > Feed for podcast metadata. See [Environment variables](environment-variables.md) for the full list.

| Variable | What it is | Example |
|---|---|---|
| `BASE_URL` | Public-facing URL for the feed and media | `https://podcast.example.com` |
| `FEED_TITLE` | Podcast title | `Drew's reading list` |
| `FEED_AUTHOR` | Author / itunes:author | `Drew K.` |
| `FEED_EMAIL` | Owner email (required by Apple) | `you@example.com` |
| `FEED_CATEGORY` | iTunes category ([valid list](feeds-and-podcasting.md#valid-itunes-categories)) | `Technology` |
| `FEED_LANGUAGE` | RFC 5646 tag | `en-US` |
| `LLM_PROVIDER` | One of the four [providers](llm-providers.md) | `openai-compatible` |
| `LLM_MODEL` | A model available from your configured provider | Select from the model list |

The admin password lives under Settings > System > Admin access (a bcrypt hash in the DB); until it is set the app runs in open convenience mode, and warns you about it when the server looks internet-facing.

For local HTTP access, turn off Secure session cookie under System > Security before setting a password. Keep it enabled when serving the app over HTTPS.

## Extraction out of the box

Extraction works with no extra services: the default `direct` engine fetches the page in-process and parses it with trafilatura. To use a self-hosted [Firecrawl](https://github.com/firecrawl/firecrawl) instead, set `EXTRACTION_ENGINE=firecrawl` and point `FIRECRAWL_URL` at it. Either way, JS-rendered and bot-gated pages fall back through the cascade described in [Paywalled articles](paywalls.md).

[< Docs index](README.md)
