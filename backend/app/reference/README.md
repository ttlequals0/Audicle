# Reference Voice (`voice.wav`)

The XTTS-v2 wrapper conditions on a single short voice sample to colour every
synthesis call. Drop your clip at `backend/app/reference/voice.wav`; the
container bind-mounts it read-only into the wrapper at
`/app/reference/voice.wav`.

## Spec

| Property | Recommended | Hard limits (enforced by `/api/v1/reference/commit`) |
|---|---|---|
| Format | WAV (PCM, 16-bit) | WAV that Python's `wave` module can read |
| Channels | mono | mono or stereo (XTTS mixes down internally) |
| Sample rate | 24 kHz | 16-48 kHz |
| Duration | 8-12 seconds | 3-60 seconds |
| Loudness | -20 to -16 LUFS | n/a |
| Content | clean speech, no background music, no silence padding | n/a |
| Size | ~250 kB - 1 MB | <= 5 MB |

## How to source one

Three reasonable paths:

1. **Record yourself**: phone voice memo in a quiet room, then trim with
   `ffmpeg -i raw.m4a -ac 1 -ar 24000 -ss 3 -to 13 -acodec pcm_s16le voice.wav`.
   Read a few sentences with neutral cadence; the wrapper picks up your
   prosody more than your vocabulary.
2. **Reuse a creative-commons voice** from the LibriVox / LJSpeech / VCTK
   corpora. Verify the licence allows derivative work (LJSpeech and VCTK
   are CC0 / CC BY 4.0).
3. **Synthesize one** from a paid voice provider you have rights to.
   Don't use a celebrity voice you don't own; the XTTS weights happily
   reproduce it.

## Licence note

The XTTS-v2 model weights are governed by the Coqui Public Model Licence
1.0.0 (CPML). It is non-commercial. Personal self-hosted use is fine; do
not redistribute generated audio commercially without checking the licence
text in the `tts-wrapper/` directory.

## Verifying

```bash
ffprobe -hide_banner voice.wav
# Expect: pcm_s16le, mono, 24000 Hz, ~10s
```

The admin UI's Settings page has a "reference voice" widget that previews
the installed clip, lets you upload a candidate, auditions it against
sample text via `POST /api/v1/reference/test`, and atomically swaps the
live clip via `POST /api/v1/reference/commit` (which also triggers the
wrapper's `/reload`).
