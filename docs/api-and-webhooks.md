# API and webhooks

The API lives under `/api/v1`, with interactive docs at `/api/v1/docs` (linked from Settings > system info) and a checked-in schema at `openapi.yaml`. Mutating endpoints need the session cookie plus the CSRF double-submit header; until an admin password is set, the app runs in open convenience mode.

## The surface, by area

| Area | Endpoints |
|---|---|
| Submit | `POST /submit` (URL), `POST /upload` (one file per request; the UI loops for batches), `POST /upload/{episode_id}/reprocess` |
| Jobs | `GET /jobs`, `POST /jobs/{id}/requeue`, `POST /jobs/{id}/cancel` |
| Episodes | `GET /episodes?page=&per_page=&q=` (paginated, `X-Total-Count` header carries the filtered total; `q` matches title, source URL, and uploaded filename), `DELETE /episodes/{id}`, `POST /episodes/{id}/chapters` |
| Feed | `GET/POST /feed-auth`, `POST /feed-auth/regenerate`, `POST /feed/recreate?confirm=true` (global GUID reset: every subscriber re-downloads) |
| Settings | `GET/PUT /settings` (the runtime allowlist), `GET/PUT /prompt`, corrections, source-fallbacks, reference voice slots, chime |
| Ops | `POST /purge?confirm=true&older_than_days=N`, `POST /webhooks/test` |
| Health | `GET /health/live`, `GET /health/ready` (outside `/api/v1`) |

Media is served under `/media/`: `{id}.mp3`, `{id}.vtt`, `{id}.chapters.json`, `{id}.txt` (the cleaned article), `{id}.jpg`.

## Webhooks

Audicle can POST a JSON payload to a URL of yours every time an episode finishes (`episode.processed`) or fails (`episode.failed`): handy for a Slack/Discord ping, a dashboard, or a downstream trigger. Set `WEBHOOK_URL` in Settings (the Webhooks group); leave it blank to turn it off. The "send test webhook" button fires a sample at the saved URL and shows the response, so you can wire up a receiver first.

Payload fields:

| Field | Type | When | Meaning |
|---|---|---|---|
| `event` | string | always | `episode.processed` or `episode.failed` |
| `episode_id` | string | always | the episode's stable id |
| `title` | string | always | episode title (falls back to the filename or URL) |
| `voice` | string | always | the reference voice that narrated it: a slot label, `Slot N`, or `Default` |
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

An upload episode is the same shape with `"source_type": "upload"` and a `"source_filename"` instead of `url`. The test button's payload adds `"test": true` so your receiver can tell it from a real run.

Delivery is fire-and-forget: a background task with a short timeout (`WEBHOOK_TIMEOUT_SECONDS`, default 10s) and a few retries with backoff, so a dead or slow receiver never delays or fails the episode. To test from the API, `POST /api/v1/webhooks/test` returns `{ "delivered", "status_code", "error" }`.

[< Docs index](README.md)
