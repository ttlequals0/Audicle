# Voices and TTS

## Reference voice slots

The wrapper narrates each episode by conditioning on a short reference clip you supply. Manage clips in Settings under "voices": five labelled slots. Each row plays its stored clip and can audition a TTS sample, so you hear a voice before you use it.

Each episode picks a voice at submit time from the picker under the Submit button on Home: Random (a random filled slot), Last used, or a specific slot. At least one slot must stay filled: submit and upload are rejected with a 400 until a voice is loaded (unless TTS runs on a [remote backend](#running-tts-on-another-host), which manages its own voices). A job with no recorded voice falls back to the lowest filled slot at synthesis time.

Recommended clip: mono, 24 kHz, 8 to 12 seconds, roughly 250 kB to 1 MB. Upload limits are 3 to 60 seconds and 5 MB. WAV, MP3, M4A/AAC, FLAC, and OGG/Opus are accepted; anything that is not already a WAV is converted with ffmpeg before storage. See `backend/app/reference/README.md` for the sourcing playbook.

Output quality mostly tracks clip quality. Cleaning the source (noise reduction, leveling) helps more than any TTS knob.

## Models and languages

The TTS model and narration language are switchable in Settings under TTS: `chatterbox` (English, the default) or `chatterbox-multilingual`. Both apply on the next episode with no restart, and the dropdowns lock while a job is running so an episode never changes voice partway through.

Generation tuning (temperature, repetition penalty, top-p, top-k, seed, per-call character cap) lives in the TTS generation group and rides on every request to the wrapper, so a change applies to the next job.

## End-of-episode chime

Settings has an "end chime" section: upload one short clip that plays at the end of every episode, so back-to-back episodes are easy to tell apart on autoplay. Turn it on with the toggle in that same section (`CHIME_ENABLED`); the clip is transcoded and loudness-matched to the narration. Upload WAV/MP3/M4A/FLAC/OGG, trimmed to about 15 seconds. Delete it to stop.

## Running TTS on another host

By default the bundled wrapper does the synthesis (`TTS_BACKEND=wrapper`). If you already run Chatterbox (or any speech server with an OpenAI-compatible API) on another machine, point Audicle at it:

| Setting | What it is |
|---|---|
| `TTS_BACKEND` | `wrapper` or `openai-api` |
| `TTS_API_BASE_URL` | The remote server's `/v1` endpoint |
| `TTS_API_KEY` | Bearer token, if the server wants one (stored masked) |
| `TTS_API_MODEL` | Model name to request |
| `TTS_API_VOICE` | Voice name on the remote server |

The remote server manages its own voices, so the local voice slots do not apply and `TTS_API_VOICE` names the one to use. The client receives audio bytes and writes them where the pipeline expects, so everything downstream (audio analysis, chapters, transcripts) works identically.

One consequence: a remote speech endpoint returns audio only, no transcript, so ASR verification cannot come from the bundled wrapper. That is what `WHISPER_BACKEND` is for.

## ASR verification

Verification transcribes each generated chunk and compares the transcript to the text that was requested; a chunk that diverges too far is regenerated. How the comparison works is covered in [How it works](how-it-works.md#the-quality-gates).

`WHISPER_BACKEND` selects where the transcription runs:

| Value | Where ASR runs | Needs |
|---|---|---|
| `wrapper` (default) | The bundled wrapper transcribes what it just synthesized | `WHISPER_ENABLED=true` on the wrapper (loads the model at its startup) plus `WHISPER_VERIFY_ENABLED=true` on the backend. No GPU required: the Whisper device follows `TTS_DEVICE`, float16 on CUDA and int8 on CPU, so a CPU wrapper verifies too, just slower per chunk |
| `openai-api` | A remote OpenAI-compatible `/v1/audio/transcriptions` | `WHISPER_API_BASE_URL` (and optionally a key, model, timeout) |
| `off` | Nowhere; verification is skipped | nothing |

The two backends are independent: local synthesis with remote transcription is a valid pairing. The one combination that cannot work, remote synthesis with wrapper verification, is rejected at startup with a message naming the fix. The reason is plumbing, not hardware: the bundled wrapper transcribes what it just synthesized, and audio made on another host never passes through it, so it has nothing to transcribe.

A transcription failure normally never fails a chunk that synthesized fine; the chunk just goes unverified, the same as the wrapper path behaves. On the remote backend, `WHISPER_API_STRICT` (default off) reverses that: a chunk whose transcription cannot be obtained fails instead of shipping unverified. Turn it on when an unverified chunk is worse for you than a failed job, for example on feeds where a hallucinated run slipping through matters more than latency.

The strictness knobs (divergence threshold, divergent-run length, minimum words, short-chunk bar) are live-tunable in the Verification group.

[< Docs index](README.md)
