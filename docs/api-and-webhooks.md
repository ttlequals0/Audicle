# API and webhooks

The API lives under `/api/v1`, with interactive docs at `/api/v1/docs` (linked from Settings > System > System info) and a checked-in schema at `openapi.yaml`. Every `/api/v1` endpoint except `/auth/*` needs the session cookie once an admin password is set. Mutating requests also need the CSRF double-submit header (`X-CSRF-Token`, echoing the `audicle_csrf` cookie or the `csrf_token` from login). Without a password the app runs in open convenience mode and needs neither. A missing or expired session returns 401, a bad CSRF token 403.

## Sidecar settings

`GET /api/v1/settings/sidecars` returns `render` and `tts_wrapper` groups. Each contains saved `values`, environment/code `defaults`, `available`, and `pending`. Defaults are empty when the sidecar cannot be reached; saved overrides remain visible.

`PUT /api/v1/settings/sidecars` accepts partial updates grouped by sidecar. For example:

```json
{"render":{"RENDER_ATTEMPTS":2},"tts_wrapper":{"TTS_IDLE_UNLOAD_SECONDS":600}}
```

Send `null` for an individual setting to reset it. The app stores desired values in its database and reconciles them with the sidecars. Before claiming a queued job, the worker applies saved wrapper settings. For wrapper TTS, jobs with saved settings stay queued until those settings can be applied. A wrapper restart during an active job restores its environment defaults until the next job sync. Keep intentional startup values such as `WHISPER_ENABLED` in Compose. Rendering options apply per extraction request; logging and wrapper controls apply live. These endpoints require the same administrator session and CSRF token as other settings changes. The sidecars' internal `/runtime-config` endpoints must not be exposed publicly.

## Common endpoints

| Area | Endpoints |
|---|---|
| Auth | `GET /auth/status`, `POST /auth/login` (rate limited; 423 while locked out), `POST /auth/logout`, `PUT /auth/password`, `POST /auth/revoke-sessions` |
| Submit | `POST /submit` (URL), `POST /upload` (one file per request; the UI loops for batches), `POST /upload/{episode_id}/reprocess`, `GET /status/{job_id}` |
| Books | `GET /books`, `GET /books/{book_id}`, `POST /books/{book_id}/chapters`, `DELETE /books/{book_id}` |
| Jobs | `GET /jobs?status=&page=&per_page=` (`X-Total-Count` header), `POST /jobs/{id}/requeue`, `POST /jobs/{id}/cancel`, `DELETE /jobs/{id}`, `DELETE /jobs?scope=all` or `failed` (finished runs only; queued and processing jobs are kept) |
| Episodes | `GET /episodes?page=&per_page=&q=` (paginated, `X-Total-Count` header carries the filtered total; `q` matches title, source URL, and uploaded filename), `DELETE /episodes/{id}`, `POST /episodes/{id}/chapters` |
| Feed | `GET/POST /feed-auth`, `POST /feed-auth/regenerate`, `POST /feed/recreate?confirm=true` (global GUID reset: every subscriber re-downloads; returns `podcast_guid` and `guid_epoch`) |
| Settings | `GET/PUT /settings` (the runtime allowlist), `GET/PUT /settings/sidecars`, `GET /settings/ocr/languages`, `GET/PUT/DELETE /prompt?kind=` (`cleanup`, `summary`, `pronunciation`, `chapters`), `/corrections` (GET, PUT, DELETE, plus `/seed`, `/lookup`, `/export`), `GET/PUT /source-fallbacks`, `POST /source-fallbacks/test` |
| Voice | `/reference/slots` (list, upload, delete, rename, preview, audition), `/chime` (GET, POST, DELETE, `/preview`), `GET /tts/models` |
| LLM | `GET /llm/models`, `POST /llm/models/refresh`, `POST /llm/test` |
| Ops | `POST /purge?confirm=true&older_than_days=N` (default `0` wipes every episode; returns the removed ids), `POST /webhooks/test` |
| Health | `GET /health/live`, `GET /health/ready` (also `/health`), `GET /health/ingestion` (outside `/api/v1`, no login) |

`POST /submit` and `POST /upload` take an optional `voice` (a slot number 1 to 5, `last`, or `random`) and a `reprocess` flag; a duplicate source returns 409 unless `reprocess` is true. Both return 400 until a voice slot is filled, unless TTS runs on a [remote backend](voices-and-tts.md#running-tts-on-another-host).

The interactive docs and `openapi.yaml` list every route and its request schema.

## Books and chapter splitting

`POST /upload` accepts `.epub` as well as the other document types. Two optional form fields turn an EPUB or PDF into a chapter-split book:

| Field | Meaning |
|---|---|
| `split_chapters` | `true` to split into one episode per chapter. Default `false`. Any type other than EPUB or PDF returns 400. |
| `chapters` | Which chapters to queue: `all`, `default`, or a list such as `1,3,5-9`. Requires `split_chapters` (400 otherwise). |

| Case | Result |
|---|---|
| `split_chapters` false | One episode, as before. |
| Chapters found, no `chapters` field | 201 with the book and its chapter list; nothing is queued. |
| Chapters found, `chapters` given | 201 with the book; those chapters are queued in the same call. |
| Same file uploaded again with `split_chapters` | 200 with the existing book and the current chapter status; nothing is duplicated. |
| No chapters found | One episode is queued, and the response is the usual `SubmitResponse` with `"chapters_detected": false`. |

An invalid archive, a DRM-protected EPUB, an archive over the size or entry limits, or a bad `chapters` value returns 400. Chapters come from the EPUB table of contents or the PDF bookmark outline; top-level entries only. See [How it works](how-it-works.md#books-and-chapters) for detection and limits.

```bash
curl -b cookies.txt -H "X-CSRF-Token: $CSRF" \
  -F "file=@book.epub" -F "split_chapters=true" \
  http://your-server:8000/api/v1/upload
```

```json
{
  "book_id": "b1c2d3e4f5a6",
  "title": "Example Book",
  "author": "A. Author",
  "filename": "book.epub",
  "format": "epub",
  "season_number": 1,
  "has_cover": true,
  "voice_id": null,
  "created_at": "2026-11-21T09:30:00Z",
  "counts": {"not_queued": 27, "queued": 0, "processing": 0, "done": 0, "failed": 0, "cancelled": 0, "removed": 0},
  "can_resume": false,
  "chapters": [
    {"index": 1, "title": "Prologue", "word_count": 1952, "default_selected": true,
     "selectable": true, "short": false, "requested": false, "status": "not_queued", "episode_id": "c7d8e9f0a1b2"}
  ],
  "queued": [],
  "skipped": []
}
```

`GET /books` returns the same objects with `chapters` as an empty list. `title` is the title stored in the file, or the filename without its extension when the file has none. `voice_id` is the voice slot the book narrates in, or null until chapters are first queued. A chapter's `status` is one of `not_queued`, `queued`, `processing`, `done`, `failed`, `cancelled` or `removed`. It is read from the chapter's newest job; `removed` means the episode was published and has since been deleted by you or by retention.

`POST /books/{book_id}/chapters` queues chapters and requires a loaded voice, like upload:

```json
{"chapters": "remaining", "voice": "2"}
```

| `chapters` | Queues |
|---|---|
| `all` | every selectable chapter, including short ones |
| `default` | the chapters ticked by default |
| `remaining` | chapters you queued earlier that are not done and not running (failed, cancelled), skipping any chapter whose episode was deleted after it was published |
| `[1,3,5]` or `"1,3,5-9"` | those chapters |

The response is the book with `queued` (chapter numbers) and `skipped` (chapters already done or in flight, not selectable, or already requested by another call). A failed or cancelled chapter listed by number is queued again. A chapter number out of range, a chapter with no text named explicitly, or a malformed value returns 400; an unknown book returns 404. A book narrates in one voice: `voice` is stored on the book the first time chapters are queued, and later calls reuse it unless `voice` is given.

`DELETE /books/{book_id}` returns 204, or 409 while any chapter is queued or processing. It removes the book and its stored original and cover. Published chapter episodes stay in the feed but can no longer be reprocessed. To re-run one chapter, use `POST /upload/{episode_id}/reprocess` or `POST /jobs/{id}/requeue`; both read the source from the book.

In `GET /jobs`, a chapter job has `source_filename` set to the book's filename and `chapter` set to its number. `chapter` is null for every other job.

The feed is served at `/rss/{slug}.xml` and media under `/media/`: `{id}.mp3`, `{id}.vtt`, `{id}.chapters.json`, `{id}.txt` (the cleaned article), and `{id}.jpg`. Both are outside `/api/v1` and, with [authenticated feeds](feeds-and-podcasting.md#authenticated-feeds) on, need the feed key. Published URLs add `?v=<generation>` to non-artwork media; artwork carries the generation in its filename.

## Webhooks

Audicle can POST a JSON payload to a URL of yours every time an episode finishes (`episode.processed`) or fails (`episode.failed`), for example to ping Slack or start a downstream job. Set `WEBHOOK_URL` under Settings > Services > Webhooks; leave it blank to turn it off. The "Send test webhook" button fires a sample at the saved URL and shows the response, so you can wire up a receiver first.

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
| `chapter` | integer or null | upload jobs | the chapter number for a job from a chapter-split book, else null |
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

An upload episode is the same shape with `"source_type": "upload"` and a `"source_filename"` and `"chapter"` instead of `url`. The test button's payload adds `"test": true` so your receiver can tell it from a real run.

Delivery is fire-and-forget: a background task with a timeout (`WEBHOOK_TIMEOUT_SECONDS`, default 10 s), so a dead or slow receiver never delays or fails the episode. A connection error or a 5xx response is retried up to 3 attempts in total with backoff; a 4xx response is logged and not retried. Payloads are not signed. `POST /api/v1/webhooks/test` sends the sample once, with no retries, and returns `{ "delivered", "status_code", "error" }`, or 409 when no `WEBHOOK_URL` is set.

[< Docs index](README.md)
