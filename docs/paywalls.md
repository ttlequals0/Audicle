# Paywalled articles

Some sites serve a scraper only a teaser and hide the rest behind a paywall. The teaser looks like a real article but makes a 25-second junk episode. The "site overrides" section in Settings routes those hosts through a bypass strategy.

Pick a default strategy and a teaser threshold, then add per-site overrides. The default applies to any host whose scrape comes back near-empty (below `MIN_EXTRACTION_CHARS`, a hard block that returned almost nothing). A per-site rule overrides it with its own strategy and a higher teaser threshold, so a partial teaser that clears the global floor still triggers a retry; set a host to `none` to opt out. If the retry still falls short, the job fails cleanly instead of narrating the stub. Articles above the floor are left alone. Same config behind `GET`/`PUT /api/v1/source-fallbacks`.

## The strategies

- `googlebot` (the default): re-fetch the same URL as Googlebot (crawler UA + `X-Forwarded-For`). SEO-metered paywalls serve the crawler the full article, so this works most often. With the default direct engine it is a second in-process GET carrying those headers; with Firecrawl it rides the scrape. No extra container.
- `freedium`: rewrite the URL to a Freedium reader proxy. Best for Medium.
- `custom`: rewrite to your own reader-proxy template (any URL containing `{url}`).
- `reader`: fetch through the [Jina Reader](https://jina.ai/reader) proxy, which returns clean markdown and clears DataDome/PerimeterX bot walls that FlareSolverr cannot; those answer a scrape with a 401 challenge, not a real page. Set the endpoint with `READER_PROXY_TEMPLATE` (must contain `{url}`). The keyless public endpoint is rate limited; if it returns empty or truncated bodies, get a free key at jina.ai/reader and set `READER_API_KEY`. Both are live-tunable in Settings > Connections (the key is stored masked). Audicle asks Jina for a live fetch every time, not a copy from its shared cache, which can be stale or belong to a different page.
- `flaresolverr`: fetch through your FlareSolverr (a real browser) instead of the scraper, for hosts that hard-block the scraper's datacenter IP with a 403. Needs `FLARESOLVERR_URL`. Audicle already does this automatically on any hard block (below), so the per-host setting is mainly an explicit override, e.g. to force the solver on a host that returns a teaser rather than an empty page. A `flaresolverr` rule can carry a cookie jar (below) for sites you subscribe to.
- `render`: load the page in the bundled render sidecar's headful browser, which clicks "EXPAND TO CONTINUE READING" gates until the body stops growing. Some sites hide the second half behind that click (behind DataDome); FlareSolverr clears the challenge but its headless browser cannot click, so it returns only the front half. A render rule tries the render browser first, with its cookie jar if set. Without a rule, render is the last attempt on a hard block, a challenge, or a registration wall, and a page that still looks truncated triggers it as enrichment. Set `RENDER_URL` (empty disables it); the sidecar is internal-only. DataDome is probabilistic, so a render that hits a CAPTCHA falls back to the front-half partial and logs it.
- `archive`: pull a saved copy from a public archive. Tries the [Wayback Machine](https://web.archive.org) first (a clean API, no bot wall, no cookies), then archive.today through FlareSolverr. Good for a metered or soft wall, or an old article archived while it was still free. Not a way past a hard subscriber wall: if no free copy was ever archived, there is nothing to fetch.
- `none`: do not try anything. A matched host that comes back short just fails, which is what you want for a hard paywall you would rather skip than narrate.

A Medium-to-Freedium rule ships on by default; your own rules layer on top and win on host collision. The whole feature is gated by `EXTRACTION_FALLBACKS_ENABLED` (set it false for direct scrapes only, no default-proxy retry).

## Teaser detection

Some sites pad a one-paragraph teaser with "Recommended For You" and "Latest News" rails, so the scraped text clears the threshold on chrome alone. For a host with a rule, Audicle measures the page's JSON-LD `articleBody` length instead, so the lede is caught and routed to the bypass. Pages recovered by FlareSolverr or the Wayback Machine get the same check. Every page is also measured by its sentence-like text. A result that is only menus, share links, or a bot-wall notice counts as empty, and the next method runs. The "test a URL" button runs your rules against one link and reports the character count and matched strategy, which is also the quickest way to confirm a cookie jar still works.

## Hard blocks

On top of any per-host strategy, a short scrape escalates on its own. The cheap rungs go first:

1. The host's own strategy (for most hosts that is the `googlebot` default).
2. FlareSolverr, when the scrape came back near-empty (a 403 or IP block) and `FLARESOLVERR_URL` is set. Audicle does not bundle a solver.
3. The reader proxy (`READER_AUTO_ENABLED`, on by default).
4. A public archive: the newest Wayback captures, then archive.today through FlareSolverr (`ARCHIVE_FALLBACK_ENABLED`, on by default). This also runs for a teaser, and the capture has to clear the host's teaser threshold.

The exception is a Cloudflare challenge page ("Just a moment...", a Ray ID). That goes straight to FlareSolverr, because Cloudflare checks that a Googlebot request really comes from Google, so the header trick cannot get through.

The automatic reader rung sends the article URL to the reader proxy, which is Jina's hosted service unless you changed `READER_PROXY_TEMPLATE`. Turn it off with the `reader_auto_enabled` toggle in Settings > Extraction, `PUT /api/v1/settings` with `{"READER_AUTO_ENABLED": false}`, or the env var. Hosts with an explicit `reader` rule still use it.

## Registration walls

Some walls only want an email. A publisher shows two paragraphs, then "Continue Reading This Story for FREE!" and a signup form, and an anonymous reader never gets the rest. Audicle cuts the body at that prompt, so the signup furniture and the author bio are not narrated, and holds what remains to twice the usual floor; an article that does not survive that fails the job with a message saying so, instead of publishing a 40-second stub.

Set `REGISTRATION_EMAIL` (Settings > Extraction, or env) and the render sidecar answers the form with that address in its own browser, then re-reads the unlocked page. It needs `RENDER_URL`, runs only on a detected wall, and only after the ordinary bypasses came up short. The form has to ask for an email, ask for no password, and carry registration wording, so a login, a comment box, or a footer newsletter signup never receives the address. One submission per article, however many times the render retries. With no address set anywhere, nothing is ever submitted; clearing the field in Settings drops the override and falls back to whatever `REGISTRATION_EMAIL` holds in the environment, so blank both to switch it off. The address does reach the publisher, and a site that confirms by email before unlocking still will not open, so treat it as best effort.

## Subscriber paywalls (cookie jar)

Some walls never serve the body to a logged-out request, no matter the IP: every anonymous reader gets the same teaser, so even a fresh FlareSolverr session gets nothing more. If you pay for the site, point the host at the `flaresolverr` or `render` strategy and paste your logged-in session cookies into its cookie jar (`name=value; name2=value2`, copied from your browser). The browser then fetches the article as you. Use `render` for sites behind DataDome, such as wsj.com. FlareSolverr cannot clear DataDome; the render sidecar's browser can. A render rule with cookies goes to it first.

A session cookie is full account access, so use a dedicated login where the site allows one and treat the jar like a password. Audicle holds it with the other secrets, never logs it, and reads it back masked once saved: re-saving the masked value keeps the stored cookies, clearing the field removes them. Needs `FLARESOLVERR_URL` or `RENDER_URL` set, to match the strategy.

## When it still fails

The job says why: a hard block with no solver points you at `FLARESOLVERR_URL`; a hard block the solver could not clear means the site needs a login; a short teaser means add a per-host bypass.

## Credit

The bypass strategies are inspired by [Ladder](https://github.com/everywall/ladder). Audicle does not run Ladder or depend on it; the Googlebot fetch is reimplemented natively here. Credit to that project for the technique.

[< Docs index](README.md)
