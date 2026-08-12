# Paywalled articles

Some sites serve a scraper only a teaser and hide the rest behind a paywall. The teaser looks like a real article but makes a 25-second junk episode. The "site overrides" section in Settings routes those hosts through a bypass strategy.

Pick a default strategy and a teaser threshold, then add per-site overrides. The default applies to any host whose scrape comes back near-empty (below `MIN_EXTRACTION_CHARS`, a hard block that returned almost nothing). A per-site rule overrides it with its own strategy and a higher teaser threshold, so a partial teaser that clears the global floor still triggers a retry; set a host to `none` to opt out. If the retry still falls short, the job fails cleanly instead of narrating the stub. Articles above the floor are left alone. Same config behind `GET`/`PUT /api/v1/source-fallbacks`.

## The strategies

- `googlebot` (the default): re-fetch the same URL as Googlebot (crawler UA + `X-Forwarded-For`). SEO-metered paywalls serve the crawler the full article, so this works most often. It runs as scrape headers, not a separate container.
- `freedium`: rewrite the URL to a Freedium reader proxy. Best for Medium.
- `custom`: rewrite to your own reader-proxy template (any URL containing `{url}`).
- `reader`: fetch through the [Jina Reader](https://jina.ai/reader) proxy, which returns clean markdown and clears DataDome/PerimeterX bot walls that FlareSolverr cannot; those answer a scrape with a 401 challenge, not a real page. Set the endpoint with `READER_PROXY_TEMPLATE` (must contain `{url}`). The keyless public endpoint is rate limited; if it returns empty or truncated bodies, get a free key at jina.ai/reader and set `READER_API_KEY`. Both are live-tunable in Settings > Connections (the key is stored masked).
- `flaresolverr`: fetch through your FlareSolverr (a real browser) instead of the scraper, for hosts that hard-block the scraper's datacenter IP with a 403. Needs `FLARESOLVERR_URL`. Audicle already does this automatically on any hard block (below), so the per-host setting is mainly an explicit override, e.g. to force the solver on a host that returns a teaser rather than an empty page. A `flaresolverr` rule can carry a cookie jar (below) for sites you subscribe to.
- `render`: load the page in the bundled render sidecar's headful browser, which clicks "EXPAND TO CONTINUE READING" gates until the body stops growing. Some sites hide the second half behind that click (behind DataDome); FlareSolverr clears the challenge but its headless browser cannot click, so it returns only the front half. Render runs after the cascade, as enrichment when FlareSolverr got a partial and as a rescue when the cascade was blocked entirely, and a page that still looks truncated triggers it even without a rule. Set `RENDER_URL` (empty disables it); the sidecar is internal-only. DataDome is probabilistic, so a render that hits a CAPTCHA falls back to the front-half partial and logs it.
- `archive`: pull a saved copy from a public archive. Tries the [Wayback Machine](https://web.archive.org) first (a clean API, no bot wall, no cookies), then archive.today through FlareSolverr. Good for a metered or soft wall, or an old article archived while it was still free. Not a way past a hard subscriber wall: if no free copy was ever archived, there is nothing to fetch.
- `none`: do not try anything. A matched host that comes back short just fails, which is what you want for a hard paywall you would rather skip than narrate.

A Medium-to-Freedium rule ships on by default; your own rules layer on top and win on host collision. The whole feature is gated by `EXTRACTION_FALLBACKS_ENABLED` (set it false for direct scrapes only, no default-proxy retry).

## Teaser detection

Some sites pad a one-paragraph teaser with "Recommended For You" and "Latest News" rails, so the scraped text clears the threshold on chrome alone. For a host with a rule, Audicle measures the page's JSON-LD `articleBody` length instead, so the lede is caught and routed to the bypass. The "test a URL" button runs your rules against one link and reports the character count and matched strategy, which is also the quickest way to confirm a cookie jar still works.

## Hard blocks

Hard blocks are handled automatically, not as a per-host strategy. With `FLARESOLVERR_URL` set (env or live in Settings), Audicle re-fetches any host through your [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr), a real browser, and pulls the article from the solved HTML. It fires on a scrape that looks like a Cloudflare challenge ("Just a moment...", a Ray ID), or a near-empty scrape (a 403/IP block). A real article or a partial teaser never triggers a solve. Audicle does not bundle a solver. As a last resort it tries a Wayback capture before failing (`ARCHIVE_FALLBACK_ENABLED`, on by default).

## Registration walls

Some walls only want an email. A publisher shows two paragraphs, then "Continue Reading This Story for FREE!" and a signup form, and an anonymous reader never gets the rest. Audicle cuts the body at that prompt, so the signup furniture and the author bio are not narrated, and holds what remains to twice the usual floor; an article that does not survive that fails the job with a message saying so, instead of publishing a 40-second stub.

Set `REGISTRATION_EMAIL` (Settings > Extraction, or env) and the render sidecar answers the form with that address in its own browser, then re-reads the unlocked page. It needs `RENDER_URL`, runs only on a detected wall, and only after the ordinary bypasses came up short. The form has to ask for an email, ask for no password, and carry registration wording, so a login, a comment box, or a footer newsletter signup never receives the address. One submission per article, however many times the render retries. With no address set anywhere, nothing is ever submitted; clearing the field in Settings drops the override and falls back to whatever `REGISTRATION_EMAIL` holds in the environment, so blank both to switch it off. The address does reach the publisher, and a site that confirms by email before unlocking still will not open, so treat it as best effort.

## Subscriber paywalls (cookie jar)

Some walls never serve the body to a logged-out request, no matter the IP: every anonymous reader gets the same teaser, so even a fresh FlareSolverr session gets nothing more. If you pay for the site, point the host at the `flaresolverr` strategy and paste your logged-in session cookies into its cookie jar (`name=value; name2=value2`, copied from your browser). The solver then fetches the article as you.

A session cookie is full account access, so use a dedicated login where the site allows one and treat the jar like a password. Audicle holds it with the other secrets, never logs it, and reads it back masked once saved: re-saving the masked value keeps the stored cookies, clearing the field removes them. Needs `FLARESOLVERR_URL` set.

## When it still fails

The job says why: a hard block with no solver points you at `FLARESOLVERR_URL`; a hard block the solver could not clear means the site needs a login; a short teaser means add a per-host bypass.

## Credit

The bypass strategies are inspired by [Ladder](https://github.com/everywall/ladder). Audicle does not run Ladder or depend on it; the Googlebot fetch is reimplemented natively here as scrape headers. Credit to that project for the technique.

[< Docs index](README.md)
