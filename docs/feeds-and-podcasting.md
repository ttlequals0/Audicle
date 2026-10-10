# Feeds and Podcasting 2.0

## The feed

The RSS feed is served at a slug derived from the feed name: `FEED_TITLE="Articles of Interest"` becomes `/rss/articles_of_interest.xml`. The Feed page always shows the exact URL to paste into a podcatcher. Renaming the feed changes the slug and mints new feed and episode GUIDs, so subscribers resubscribe to the new URL.

Episodes carry a Podcasting 2.0 `podcast:transcript` (WebVTT), `podcast:chapters` (JSON), and the usual iTunes tags. A successful reprocess publishes a new immutable generation. Its token appears in the episode GUID, the media query string, and the artwork filename, so clients fetch one consistent set of audio, artwork, transcript, and chapters. A failed or cancelled reprocess leaves the previous generation published.

## Authenticated feeds

The feed is open by default: anyone with the URL can read it. The "authenticated feeds" section in Settings puts a 64-hex key on every feed and media URL: `?key=<key>` on the RSS, MP3, transcript, chapters, and text URLs, and `/media/<id>-v<generation>-<key>.jpg` for artwork. Podcast apps can drop query strings from image URLs, so artwork carries both tokens in the path. With the toggle on, a request without a valid key gets a 401. Protected responses use `Cache-Control: private, no-store`, so a shared cache cannot keep serving a revoked key.

The Feed page shows the subscribe URL with the key included. Regenerating the key changes every protected URL, so existing subscriptions need the new feed URL. Disabling authentication removes the key from generated URLs. The same controls sit behind `GET`/`POST /api/v1/feed-auth` and `POST /api/v1/feed-auth/regenerate`.

## Episode artwork

Each episode's cover goes into the feed (`itunes:image`) and is embedded in the MP3, because some players (Pocket Casts among them) read only embedded art and ignore the feed tag. Episodes without their own cover fall back to the show image. The embedded copy is a 1400px JPEG (`EMBED_ARTWORK_SIZE_PX`) to keep file size down; the feed still serves the full 3000px master.

## Chapters

Episodes at least 10 minutes long are eligible for chapters by default. The LLM reads the full transcript text in 45-minute windows and marks topic changes, aiming for chapters around 10 minutes long. A 3-hour episode uses 4 windows, so its chapter count can grow with its length. A window continues the previous topic rather than adding a chapter just because a new window starts. Empty windows and windows shorter than the target are skipped, including a short final window. An eligible episode shorter than the target produces no chapters.

Chapter timestamps come from the measured audio timeline used by the transcript. Boundaries less than 60 seconds apart are deduplicated, and chapters shorter than the configured minimum merge into the previous chapter. The opening chapter at zero is always retained; a missing opening title becomes "Introduction". Topic targets are guidance, not quotas; an episode with fewer topic changes can have fewer chapters.

Chapters ship as both a Podcasting 2.0 `podcast:chapters` JSON document in the feed and ID3 chapter frames in the MP3, because clients differ in what they read.

The Chapters section in Settings exposes these controls:

| Setting | Default | Purpose |
|---------|---------|---------|
| `CHAPTERS_ENABLED` | `True` | Enable chapter generation |
| `CHAPTERS_MIN_DURATION_SECS` | 600 seconds | Minimum episode length eligible for chapters |
| `CHAPTERS_TARGET_SECONDS` | 600 seconds | Target chapter length |
| `CHAPTERS_WINDOW_SECONDS` | 2700 seconds | Analysis window: transcript span per LLM call |
| `CHAPTERS_MAX_BOUNDARIES` | 40 boundaries | Maximum topic changes, excluding the opening chapter |
| `CHAPTERS_MIN_CHAPTER_SECONDS` | 180 seconds | Minimum chapter length, except the opening chapter |

These values also work through `/api/v1/settings` and environment variables. The API accepts targets from 120 to 3600 seconds and windows from 600 to 10800 seconds. Caps are 1 to 200 boundaries; minimum chapter lengths are 30 to 900 seconds. The target cannot exceed the window, and the minimum cannot exceed the target. Generation clamps environment values to these ranges and relationships. Resetting a setting with `null` restores its environment or code default.

The chapter prompt is editable via `/api/v1/prompt?kind=chapters`; existing custom prompts remain in use. Reset it with `DELETE /api/v1/prompt?kind=chapters` to use the current default. Window-specific instructions supply the time range, boundary allowance, and previous topic. They take precedence over count and opening rules in custom prompts. Each analyzed window makes a separate LLM call. Larger windows require more model context.

Regenerate chapters from the Feed page without re-synthesizing audio. If any window fails after retries, regeneration keeps the existing chapters and audio. New episodes can use the successful windows. If fewer than 2 chapters survive, new episodes ship without chapters and regeneration keeps the existing chapters.

Episodes also open with a spoken "{title}. By {author}." line (`INTRO_READ_ENABLED`).

## Valid iTunes categories

Apple's parser rejects anything not on its list. The current set (from Apple's RSS spec, May 2026):

```
Arts, Business, Comedy, Education, Fiction, Government, History,
Health & Fitness, Kids & Family, Leisure, Music, News, Religion & Spirituality,
Science, Society & Culture, Sports, Technology, True Crime, TV & Film
```

Subcategories are not surfaced in the UI; set the top-level category and you are done. If Apple Podcasts shows your feed as "Unknown" after submission, it is almost always a category typo.

## Retention

A daily sweep runs from the worker at `RETENTION_SWEEP_HOUR_UTC`, deleting episodes older than `RETENTION_DAYS`. `POST /api/v1/purge?confirm=true&older_than_days=N` does the same on demand; the default `older_than_days=0` wipes every episode, so pass a cutoff when you mean one.

[< Docs index](README.md)
