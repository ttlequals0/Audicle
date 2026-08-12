# Feeds and Podcasting 2.0

## The feed

The RSS feed is served at a slug derived from the feed name: `FEED_TITLE="Articles of Interest"` becomes `/rss/articles_of_interest.xml`. The Feed page always shows the exact URL to paste into a podcatcher. Renaming the feed changes the slug and mints new feed and episode GUIDs, so subscribers resubscribe to the new URL.

Episodes carry a Podcasting 2.0 `podcast:transcript` (WebVTT), `podcast:chapters` (JSON), and the usual iTunes tags. A reprocessed episode bumps a revision that folds into its GUID, so clients re-download the regenerated audio without any manual poking.

## Authenticated feeds

The feed is open by default: anyone with the URL can read it. The "authenticated feeds" section in Settings puts a 64-hex key on every feed and media URL: `?key=<key>` on the RSS, MP3, and transcript URLs, and a `/media/<id>-<key>.jpg` path token for artwork (podcast apps drop query strings on image URLs, so art carries the key in the path). With the toggle on, a request without a valid key gets a 401.

The Feed page shows the subscribe URL with the key included. Regenerating the key (or flipping the toggle) changes every URL, so existing subscriptions break and need a resubscribe. The same controls sit behind `GET`/`POST /api/v1/feed-auth` and `POST /api/v1/feed-auth/regenerate`.

## Episode artwork

Each episode's cover goes into the feed (`itunes:image`) and is embedded in the MP3, because some players (Pocket Casts among them) read only embedded art and ignore the feed tag. Episodes without their own cover fall back to the show image. The embedded copy is a 1400px JPEG (`EMBED_ARTWORK_SIZE_PX`) to keep file size down; the feed still serves the full 3000px master.

## Chapters

Episodes 10 minutes and over get 3 to 7 chapters. One LLM call over the episode's chunk list picks the start points and short titles; timestamps come from the measured audio, so they line up with the transcript. Chapters ship two ways, because clients differ in what they read: a Podcasting 2.0 `podcast:chapters` JSON document in the feed, and ID3 chapter frames embedded in the MP3.

`CHAPTERS_ENABLED` and `CHAPTERS_MIN_DURATION_SECS` control the feature; the prompt is editable via `/api/v1/prompt?kind=chapters`. Chapters can be regenerated for a finished episode from the Feed page without re-synthesizing audio. Episodes also open with a spoken "{title}. By {author}." line (`INTRO_READ_ENABLED`).

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
