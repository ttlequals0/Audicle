# Reference voices (slots)

The Chatterbox wrapper conditions on a short voice sample to colour every synthesis
call. Since 0.35.0 voices live in five fixed slots, not a single `voice.wav`:

- On disk: `backend/app/reference/voices/slot{1..5}.wav`.
- The container bind-mounts the `reference/` dir into the wrapper, which conditions on
  its lowest filled slot at boot and switches per job via `/select-voice`.
- At least one slot must stay loaded; the API refuses to clear the last one, and
  submit/upload are rejected (400) when every slot is empty.

Upgrading from an older install: migration 018 copies a committed `reference/voice.wav`
into slot 1 on first boot (a copy, so a rollback still finds the old file).

## Adding a voice

Use the Settings page "voices" widget (recommended): upload a clip to a slot, give it a
label, and audition it against sample text. Uploads accept WAV/MP3/M4A/FLAC/OGG; the
backend transcodes anything non-WAV with ffmpeg.

Or upload directly:

```bash
curl -X POST "$BASE/api/v1/reference/slots/1" \
  -F "voice=@voice.wav" -F "label=Morgan"
```

## Clip spec

| Property | Recommended | Hard limits (enforced on upload) |
|---|---|---|
| Format | WAV (PCM, 16-bit) | any of WAV/MP3/M4A/FLAC/OGG; transcoded to WAV |
| Channels | mono | mono or stereo (mixed down internally) |
| Sample rate | 24 kHz | 16-48 kHz |
| Duration | 8-12 seconds | 3-60 seconds |
| Loudness | -20 to -16 LUFS | n/a |
| Content | clean speech, no background music, no silence padding | n/a |
| Size | ~250 kB - 1 MB | <= 5 MB |

## How to source one

Three reasonable paths:

1. **Record yourself**: phone voice memo in a quiet room, then trim with
   `ffmpeg -i raw.m4a -ac 1 -ar 24000 -ss 3 -to 13 -acodec pcm_s16le voice.wav`.
   Read a few sentences with neutral cadence; the wrapper picks up your prosody more
   than your vocabulary.
2. **Reuse a creative-commons voice** from the LibriVox / LJSpeech / VCTK corpora.
   Verify the licence allows derivative work (LJSpeech and VCTK are CC0 / CC BY 4.0).
3. **Synthesize one** from a paid voice provider you have rights to. Don't use a
   celebrity voice you don't own; the model happily reproduces it.

## Licence note

Chatterbox (Resemble AI) is MIT-licensed and embeds an inaudible PerTh watermark in its
output. Personal self-hosted use is fine; check the model's licence before
redistributing generated audio commercially.

## Verifying

```bash
ffprobe -hide_banner voice.wav
# Expect: pcm_s16le, mono, 24000 Hz, ~10s
```
