# Audicle iOS Shortcut - Design

Date: 2026-06-06

## Goal

Share an article link from any app via the iOS Share Sheet, have it submitted to
Audicle for processing, and get a notification confirming it was queued.
Fire-and-forget: the shortcut does not wait for processing to finish.

## Decisions

| Decision | Choice |
|----------|--------|
| Auth mode | Password mode (admin password is set) |
| Completion behavior | Fire-and-forget (notify on queue, no polling) |
| Credential storage | Editable Text actions at the top of the shortcut |
| Build approach | Adapt `build-shortcut.py` -> `build-audicle-shortcut.py` |

## Auth flow (password mode)

Each run performs login then submit in a single shortcut run, relying on the
shared Shortcuts cookie jar so the session cookie set during login is resent on
submit.

```
POST {server}/api/v1/auth/login   body {"password": <password>}
   -> Set-Cookie: audicle_session (signed session, httponly)
   -> Set-Cookie: audicle_csrf
   -> response JSON: { authenticated, password_set, csrf_token }

POST {server}/api/v1/submit       body {"url": <articleUrl>}
                                  header X-CSRF-Token: <csrf_token>
   -> 201 { job_id, episode_id, status }
```

Server-side requirements confirmed in code:
- `backend/app/api/v1/router.py`: every `/api/v1` route except `/auth/*` is behind
  `require_admin`.
- `backend/app/api/deps.py:require_admin`: when a password is set, a mutating
  request needs a valid `audicle_session` session plus a `X-CSRF-Token` header
  matching the `audicle_csrf` cookie (double-submit). GET/HEAD/OPTIONS skip the
  CSRF check.
- `backend/app/services/csrf.py`: cookie name `audicle_csrf`, header name
  `X-CSRF-Token`, `httponly=False`.
- `backend/app/main.py`: session cookie `audicle_session`, `same_site=lax`,
  Starlette `SessionMiddleware`.
- `backend/app/api/v1/auth.py:post_login`: returns `csrf_token` in the JSON body
  and sets both cookies.

The shortcut only manually threads `csrf_token` from the login response body into
the submit `X-CSRF-Token` header. The `audicle_session` cookie is resent
automatically by the Shortcuts cookie store; no manual cookie parsing.

## Action sequence

1. Detect Link from Share Sheet input -> `urls` -> First Item -> `articleUrl`.
2. Text action `server` = `https://YOUR-AUDICLE-HERE.example.com` (user edits once
   after import).
3. Text action `password` = `YOUR-PASSWORD-HERE` (user edits once).
4. If `urls` has any value:
   1. Get Text -> `{server}/api/v1/auth/login`; Get Contents of URL POST JSON
      `{"password": password}` -> `loginResponse`.
   2. Get Dictionary from `loginResponse` -> Value for `csrf_token` -> `csrf`.
   3. If `csrf` has no value -> Notify "Audicle: login failed" -> Stop shortcut.
   4. Get Text -> `{server}/api/v1/submit`; Get Contents of URL POST JSON
      `{"url": articleUrl}` with header `X-CSRF-Token: csrf` -> `submitResponse`.
   5. Get Dictionary from `submitResponse` -> Value for `job_id` -> `jobid`.
   6. If `jobid` has value -> Notify "Queued in Audicle".
   7. Otherwise -> Value for `detail` -> Notify "Audicle error: {detail}"
      (covers 409 already-exists and 422 validation errors).

### Plist construction notes

- URLs are built with a Get Text action, then consumed by a parameter-less Get
  Contents of URL action (the implicit-input pattern proven in
  `build-shortcut.py`'s poll loop). This avoids a variable embedded in `WFURL`,
  which `shortcuts sign` may strip.
- The `X-CSRF-Token` header uses the `WFHTTPHeaders` dictionary-field format,
  the same `WFDictionaryFieldValue` shape already used for `WFJSONValues` in the
  reference script.
- Condition codes reused from the reference script: `WFCondition` 100 = "has any
  value", 101 = "does not have any value".
- Workflow type `ActionExtension`; input content classes include URL, Article,
  and Safari web page items so the shortcut appears in the Share Sheet for links.

## Deliverables

| File | Purpose |
|------|---------|
| `build-audicle-shortcut.py` | Adapts `build-shortcut.py`; emits signed `~/Downloads/Audicle.shortcut` |
| `docs/ios-shortcut.md` | Audicle-specific setup / usage / troubleshooting; replaces the borrowed immich `ios-shortcuts.md` |
| `verify-audicle-api.sh` | curl script: login -> csrf -> submit with a cookie jar, proving the server side independent of iOS |

## Testing / verification

- Build: the script runs, `shortcuts sign` exits 0, and the unsigned plist
  re-loads via `plistlib` with all expected actions present.
- Server side: `verify-audicle-api.sh` against the live server (curl `-c`/`-b`
  cookie jar) proves login + CSRF + submit before involving iOS, isolating any
  on-device failure to Shortcuts behavior.
- On-device (user): import the `.shortcut`, edit `server` and `password`, share an
  article, confirm the "Queued" notification and that the episode appears in the
  feed.

## Risks

1. Cookie sharing - the approach assumes Shortcuts resends the `audicle_session`
   cookie set during login on the subsequent submit call. Strongly supported by
   community evidence; the #1 on-device validation point. If it fails, password
   mode via Shortcuts needs a different approach, since Shortcuts cannot easily
   read `Set-Cookie` to rebuild the header manually.
2. Header survival under signing - `WFHTTPHeaders` for `X-CSRF-Token` must
   survive `shortcuts sign`. Same dictionary-field format as the JSON body the
   reference script already relies on; confirmed at build time.
3. `detail` shape - 409 returns a string, 422 returns an array. The error
   notification stringifies whatever is present; acceptable for v1.

## Out of scope (YAGNI)

- `reprocess=true` toggle; a 409 simply surfaces "already in library".
- Status polling / completion tracking.
- Convenience-mode (no-password) branch.
