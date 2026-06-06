# Audicle iOS Shortcut

Share an article link from any app on your iPhone and have Audicle turn it into a
podcast episode. Fire-and-forget: the shortcut submits the URL and notifies you
that it was queued; the episode appears in your feed when processing finishes.

## Download

Grab the prebuilt [`Audicle.shortcut`](Audicle.shortcut) on your iOS device and
tap to import, then jump to **Set up**.

## Build it (optional)

The shortcut is generated programmatically (signed plists silently drop hand-set
parameters, so it must be built with the formats known to survive signing).

```bash
python3 build-audicle-shortcut.py
# Output: Audicle.shortcut (in this folder)
```

`shortcuts sign` (macOS) produces a signed `.shortcut` you can import on any iOS
device.

## Set up

1. AirDrop or copy `Audicle.shortcut` to your iPhone and tap to import.
2. Open the shortcut in the Shortcuts app and tap Edit.
3. Edit the two **Text** actions near the top:
   - `https://YOUR-AUDICLE-HERE.example.com` -> your Audicle server URL (no
     trailing slash).
   - `YOUR-PASSWORD-HERE` -> your Audicle admin password.
4. Done. The shortcut appears in the Share Sheet for links and web pages.

The server URL and password live inside the shortcut on your device. This is a
single-user tool; treat the shortcut like a stored credential.

## Use it

1. In Safari (or any app) open or select an article.
2. Tap Share -> **Audicle**.
3. A notification confirms "Queued for processing." The episode shows up in your
   podcast feed once Audicle finishes extraction, cleanup, and TTS.

## How it works

The shortcut runs login then submit in a single pass (password mode):

1. Detects the shared link.
2. `POST /api/v1/auth/login` with `{"password": ...}`. The server sets the
   `audicle_session` and `audicle_csrf` cookies and returns `csrf_token` in the
   response body.
3. `POST /api/v1/submit` with `{"url": ...}`. The `audicle_session` cookie is
   resent automatically by the Shortcuts cookie store; the shortcut adds the
   `X-CSRF-Token` header using the `csrf_token` from step 2.
4. If the response has a `job_id` -> "Queued for processing." Otherwise it shows
   the server's error `detail`.

It does not poll for completion -- submitting is enough; the feed does the rest.

## Verify the server side first

If the shortcut misbehaves, prove the server and request shape are fine before
debugging on-device:

```bash
AUDICLE_SERVER=https://audicle.example.com \
AUDICLE_PASSWORD=yourpassword \
AUDICLE_TEST_URL=https://example.com/some-article \
./verify-audicle-api.sh
```

It logs in with a cookie jar, echoes the CSRF token on submit, and reports PASS
on `201` (queued) or `409` (already in your library -- auth still worked). A
`401`/`403` means the session cookie or CSRF header was rejected.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| "Login failed -- check the password" | Wrong password in the Text action, or the server has no password set (convenience mode -- this shortcut targets password mode). |
| "Submit failed: login required" | The session cookie was not resent. iOS Shortcuts normally shares cookies across requests in a run; if this persists, confirm with `verify-audicle-api.sh` that the server side works. |
| "Submit failed: csrf token mismatch" | The `X-CSRF-Token` header did not survive signing or was not echoed. Rebuild with `build-audicle-shortcut.py`. |
| "Submit failed" with a 409-style message | The URL already has an episode. By design the shortcut does not reprocess; submit it from the web UI with reprocess if you want to replace it. |
| Nothing in the Share Sheet | Re-import the shortcut; make sure you are sharing a link/web page, not plain text. |

## API reference

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/auth/login` | POST | `{"password"}` -> sets session + CSRF cookies, returns `csrf_token` |
| `/api/v1/submit` | POST | `{"url"}` + `X-CSRF-Token` header -> `{job_id, episode_id, status}` (201) |
| `/api/v1/auth/status` | GET | Reports `password_set` / `authenticated` (handy to check your mode) |
