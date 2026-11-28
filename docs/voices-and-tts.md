# Voices and TTS

## Reference voice slots

The wrapper narrates each episode by conditioning on a short reference clip you supply. Manage clips under Settings > Voice > Voices: five labelled slots. Each row plays its stored clip and can audition a TTS sample, so you hear a voice before you use it.

Each episode picks a voice at submit time from the picker under the Submit button on Home: Random (a random filled slot), Last used, or a specific slot. At least one slot must stay filled: submit and upload are rejected with a 400 until a voice is loaded (unless TTS runs on a [remote backend](#running-tts-on-another-host), which manages its own voices). A job with no recorded voice falls back to the lowest filled slot at synthesis time.

Recommended clip: mono, 24 kHz, 8 to 12 seconds, roughly 250 kB to 1 MB. Upload limits are 3 to 60 seconds and 5 MB. WAV, MP3, M4A/AAC, FLAC, and OGG/Opus are accepted; anything that is not already a WAV is converted with ffmpeg before storage. See `backend/app/reference/README.md` for the sourcing playbook.

Output quality mostly tracks clip quality. Cleaning the source (noise reduction, leveling) helps more than any TTS knob.

## Models and languages

The TTS model and narration language are switchable under Settings > Voice > TTS: `chatterbox` (English, the default) or `chatterbox-multilingual`. Both apply on the next episode with no restart, and the dropdowns lock while a job is running so an episode never changes voice partway through.

Generation tuning (temperature, repetition penalty, top-p, top-k, seed, per-call character cap) lives in the TTS generation group and rides on every request to the wrapper, so a change applies to the next job.

The wrapper unloads its TTS and Whisper models after 300 idle seconds by default, releasing their GPU memory. The next synthesis request reloads the configured models before it runs. Under Settings > Services > Sidecar settings > TTS wrapper, set `TTS_IDLE_UNLOAD_SECONDS` to `0` to keep models resident, or choose another nonnegative timeout. Memory limits, inference timeout, and Whisper configuration are also adjustable there. Whisper changes wait for active synthesis to finish and load the replacement verifier when needed.

## End-of-episode chime

Under Settings > Voice > End chime, upload one short clip that plays at the end of every episode, so back-to-back episodes are easy to tell apart on autoplay. Turn it on with the toggle in that same section (`CHIME_ENABLED`); the clip is transcoded and loudness-matched to the narration. Upload WAV/MP3/M4A/FLAC/OGG, trimmed to about 15 seconds. Delete it to stop.

## Running TTS on another host

By default the bundled wrapper does the synthesis (`TTS_BACKEND=wrapper`). If you already run Chatterbox (or any speech server with an OpenAI-compatible API) on another machine, point Audicle at it:

| Setting | What it is |
|---|---|
| `TTS_BACKEND` | `wrapper` or `openai-api` |
| `TTS_API_BASE_URL` | The remote server's `/v1` endpoint |
| `TTS_API_KEY` | Bearer token, if the server wants one (masked in API responses) |
| `TTS_API_MODEL` | Model name to request |
| `TTS_API_VOICE` | Voice name on the remote server |
| `TTS_API_EXTRA_BODY` | JSON object of extra fields sent with each speech request, for example `{"speed": 1.1}`. Empty sends none |
| `TTS_API_ALLOW_PRIVATE` | Environment only. Set to `true` to allow a LAN endpoint (see below) |

The remote server manages its own voices, so the local voice slots do not apply and `TTS_API_VOICE` names the one to use. The client receives audio bytes and writes them where the pipeline expects, so everything downstream (audio analysis, chapters, transcripts) works identically.

Audicle appends `/audio/speech` to `TTS_API_BASE_URL` (and `/audio/transcriptions` to `WHISPER_API_BASE_URL` for ASR), so the base URL must include whatever prefix the server uses, for example `http://tts-host:8000/v1`.

Each speech request is a JSON POST with this body:

```json
{"model": "tts-1", "input": "<chunk text>", "voice": "alloy", "response_format": "wav"}
```

Fields from `TTS_API_EXTRA_BODY` are merged in first, so those four always win. The setting must be a JSON object of at most 4096 characters and cannot contain `model`, `input`, `voice`, or `response_format`; a save that breaks these rules is rejected. Changing it changes the TTS cache key, so earlier cached audio is not reused for the new parameters.

Audicle asks for WAV and requires the response to be a WAV container. The audio inside can use any codec ffmpeg reads (PCM, IMA ADPCM, and so on) and is converted to 16-bit PCM; a non-WAV response such as MP3 is rejected as invalid audio.

The Chatterbox generation settings (temperature, repetition penalty, top-p, top-k, seed, character cap) go only to the bundled wrapper and are never sent to a remote server. Tune a remote server on the server itself, or pass fields it understands through `TTS_API_EXTRA_BODY`.

One consequence: a remote speech endpoint returns audio only, no transcript, so ASR verification cannot come from the bundled wrapper. That is what `WHISPER_BACKEND` is for.

### Endpoint addresses

By default the endpoint must resolve to a public address. To use a server on your LAN or another container, set `TTS_API_ALLOW_PRIVATE=true` in the environment. It then also accepts `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10` (Tailscale), and `fc00::/7`. Loopback, link-local, and cloud metadata addresses are never allowed. It applies to both the TTS and ASR endpoints, and it is not editable in Settings or the API, so an admin session cannot widen it.

## ASR verification

Verification transcribes each generated chunk and compares the transcript to the text that was requested; a chunk that diverges too far is regenerated. How the comparison works is covered in [How it works](how-it-works.md#the-quality-gates).

`WHISPER_BACKEND` selects where the transcription runs:

| Value | Where ASR runs | Needs |
|---|---|---|
| `wrapper` (default) | The bundled wrapper transcribes what it just synthesized | `WHISPER_ENABLED=true` on the wrapper (loads the model when needed) plus `WHISPER_VERIFY_ENABLED=true` on the backend. No GPU required: the Whisper device follows `TTS_DEVICE`, float16 on CUDA and int8 on CPU, so a CPU wrapper verifies too, just slower per chunk |
| `openai-api` | A remote OpenAI-compatible `/v1/audio/transcriptions` | `WHISPER_API_BASE_URL` (and optionally a key, model, timeout) |
| `off` | Nowhere; verification is skipped | nothing |

The two backends are independent: local synthesis with remote transcription is a valid pairing. The one combination that cannot work, remote synthesis with wrapper verification, is rejected at startup with a message naming the fix. The reason is plumbing, not hardware: the bundled wrapper transcribes what it just synthesized, and audio made on another host never passes through it, so it has nothing to transcribe.

A transcription failure normally never fails a chunk that synthesized fine; the chunk just goes unverified, the same as the wrapper path behaves. On the remote backend, `WHISPER_API_STRICT` (default off) reverses that: a chunk whose transcription cannot be obtained fails instead of shipping unverified. Turn it on when an unverified chunk is worse for you than a failed job, for example on feeds where a hallucinated run slipping through matters more than latency.

The strictness knobs (divergence threshold, divergent-run length, minimum words, short-chunk bar) are live-tunable in the Verification group.

[< Docs index](README.md)
