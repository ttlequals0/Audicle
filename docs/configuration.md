# Configuration

Configure the app in Settings or through the authenticated API. Saved settings override legacy environment values, which override code defaults. Most processing settings apply to the next job; connection, feed, and request policies apply to subsequent requests.

An empty text field saves an explicit empty value, so it can disable an optional service even when the environment supplies a URL. Resetting a field removes its override and restores its environment or code default. The API uses `null` for reset.

Only deployment settings stay outside the UI: storage paths, worker count, session signing key, device assignment, and container networking. The [configuration reference](environment-variables.md) lists defaults and application timing. Existing environment files remain supported.

## The Settings page

Settings are grouped by the question you are answering, not by which mechanism stores them:

| Category | Groups |
|---|---|
| Content | Extraction, site overrides, Cleanup, cleanup prompt, Uploads |
| Voice | voices, TTS, TTS backend, TTS generation, pronunciation corrections, Verification, Audio analysis, Audio output, end chime, TTS delivery |
| Publishing | Feed, Artwork, Chapters, RSS, authenticated feeds, Retention |
| Services | LLM, Connections, Timeouts, Webhooks |
| System | Job timeouts, Logging, security, system info |

Search at the top filters the page as you type. The save bar appears only when the generic form has unsaved edits; the widget sections (voices, corrections, site overrides, chime, authenticated feeds, security) each save themselves. Every generic card has a reset-to-defaults control, clickable only when something in it differs from the shipped defaults.

## Content

- **Extraction**: the engine (`direct` or `firecrawl`), fetch timeouts and user agent, the extraction floor (`MIN_EXTRACTION_CHARS`), the automatic reader and archive fallbacks, the registration-wall email, and the Firecrawl content flags.
- **site overrides**: per-host paywall rules; see [Paywalled articles](paywalls.md).
- **Cleanup** and **cleanup prompt**: the floor below which a cleaned article fails, the prompt-size cap, and the editable cleanup prompt itself.
- **Uploads**: the per-file size cap and the `OCR_*` knobs for scanned documents.

## Voice

Everything that shapes how an episode sounds, in setup order: pick a [voice](voices-and-tts.md), choose the model, tune generation, check the result, shape the output.

- **TTS generation** is the Chatterbox sampling group: lower temperature reads steadier, raise the repetition penalty if words loop, seed 0 is random. It also holds the [chunk cache](how-it-works.md#the-chunk-cache) toggles (`TTS_CHUNK_CACHE_ENABLED`, `TTS_CACHE_RETENTION_DAYS`; note that seed 0 disables caching) and [adaptive chunk sizing](how-it-works.md#adaptive-chunk-sizing) (`TTS_ADAPTIVE_MAX_CHARS_ENABLED`).
- **Verification** and **Audio analysis** are the [quality gates](how-it-works.md#the-quality-gates).
- **Audio output** covers silence handling, loudness normalization, and the MP3 encode.
- **TTS delivery** is how long to keep trying when the wrapper is slow or restarting; see [the memory ladder](how-it-works.md#the-wrappers-memory-ladder) for why the connect budget must outlast a 90-second restart. The idle restart toggle (`TTS_IDLE_RESTART_ENABLED`) lives here too.

### Pronunciation corrections

A table for words the narrator mispronounces. Each row is a match term, the spoken form to say instead, a mode, and an "Aa" case toggle. A curated seed set ships built in (`GET /api/v1/corrections/seed`); your rows override it.

- spoken drives narration: write it the way you want it read ("four oh four media", "clawed").
- mode is override (say the spoken form), word (read an acronym as a word), or spell (read it letter by letter).
- Aa makes the match case-sensitive; off (the default) folds case, so a "404 media" row also catches "404 Media".

## Publishing

The podcast as subscribers receive it: feed metadata, [artwork](feeds-and-podcasting.md#episode-artwork), [chapters](feeds-and-podcasting.md#chapters), the RSS cache window, [authenticated feeds](feeds-and-podcasting.md#authenticated-feeds), and retention (`RETENTION_DAYS`; a daily sweep deletes older episodes).

## Services

- **LLM**: the [provider](llm-providers.md), model, keys, and tuning.
- **Connections**: the URLs of everything the app talks to: Firecrawl, the TTS wrapper, FlareSolverr, the render sidecar, the reader proxy. All live-tunable so you can point at an external service without an env edit and restart.
- **Timeouts**: per-request budgets for the extraction cascade. Raise one when an upstream is slow rather than losing the article.
- **Webhooks**: the notification URL and a test button; see [API and webhooks](api-and-webhooks.md#webhooks).

## System

- **Job timeouts**: when to give up on a stuck job; see [the job queue](how-it-works.md#the-job-queue).
- **Logging**: level and format, applied across web and worker processes.
- **security**: the admin password, login rate and lockout limits, proxy trust, and session cookie policy. Until a password is set the app runs in open convenience mode.
- **system info**: version, uptime, auth state, and a link to the interactive API docs.

[< Docs index](README.md)
