# Audicle TTS Wrapper

FastAPI service wrapping [Chatterbox](https://github.com/resemble-ai/chatterbox)
(Resemble AI's zero-shot voice cloning model) for narration synthesis. Audicle's
main app POSTs cleaned text chunks here and gets back WAV file paths on a shared
volume.

## License notes

- **Code**: Audicle ships this wrapper under MPL 2.0.
- **Model weights**: Chatterbox is MIT-licensed. The wrapper downloads the
  weights from Hugging Face on first run; Audicle does not redistribute them.
- **Watermark**: every output carries Resemble's inaudible PerTh watermark.
  The library has no flag to disable it.

Personal self-hosted use is fine. Check the model license before redistributing
generated audio commercially.

## Endpoints

| Method | Path           | Purpose                                                             |
|--------|----------------|---------------------------------------------------------------------|
| POST   | /generate      | Synthesize a chunk. Body: `{text, episode_id, chunk_index, seed?, verify?}`. |
| GET    | /health        | `{ok, model_loaded, reference_loaded}`. 503 until everything is ready. |
| POST   | /select-voice  | Switch the active voice to a slot. Body: `{slot}` (1-5).            |
| POST   | /reload        | Re-encode the resting voice (the lowest filled slot) into the speaker conditionals. |

## Reference voices

Voices are slots-only (since 0.35.0): there is no separate `voice.wav`. The wrapper
conditions on `reference/voices/slot{1..5}.wav`, mounted from the host. It boots on its
lowest filled slot and switches per job via `/select-voice`. Manage slots through the
app's Settings UI or `POST /api/v1/reference/slots/{n}`; see
`backend/app/reference/README.md` for the clip spec and the authoritative version.

The wrapper starts without a voice: the model loads, `/health` reports
`reference_loaded=false`, and `/generate` returns 503 until a slot is uploaded. The
first job's `/select-voice` (or a `/reload`) then encodes it with no restart. Only a
model-load failure exits the process.

## Local dev

CUDA hosts: `docker compose build tts-wrapper && docker compose up tts-wrapper`.

CPU-only hosts (5-10x slower):

```
docker build -t audicle-tts:cpu -f tts-wrapper/Dockerfile.cpu tts-wrapper/
```

Then set `TTS_DEVICE=cpu` in `.env` and pin the image name in
`docker-compose.yml` (or run the container manually).

## Model cache

The Chatterbox weights download on first run. `HF_HOME` and `TTS_HOME` default
to `/data/hf_cache` and `/data/tts_home`, so the weights persist on the mounted
`/data` volume and subsequent restarts load from disk instantly. These paths are
writable by a non-root container user (uid 1000); the old `/root/.cache`
defaults were root-only and crashed the wrapper under `user: 1000:1000`.
Throwaway compile caches (`NUMBA_CACHE_DIR`, `MPLCONFIGDIR`, `XDG_CACHE_HOME`,
`HOME`) point at `/tmp`.

## Tunable generation params

| Env var | Default | Effect |
|---|---|---|
| `CHATTERBOX_EXAGGERATION` | 0.0 | Expressiveness; baked into the reference conditionals at load. 0.0 is a neutral read. |
| `CHATTERBOX_CFG_WEIGHT` | 0.0 | Classifier-free guidance weight, applied per call. |
| `CHATTERBOX_TEMPERATURE` | 0.5 | Sampling temperature (down from Turbo's 0.8 to steady pronunciation). |
| `CHATTERBOX_SEED` | 1234 | Makes a chunk reproducible. Set 0 to disable seeding. |
| `TTS_MAX_CHARS` | 200 | Per-piece char cap; the wrapper splits each chunk into pieces under this and concatenates the audio. |
| `TTS_SAMPLE_RATE` | 24000 | Provisional output rate; replaced by the model's own rate at load. |

## GPU pinning

For multi-GPU hosts, edit `docker-compose.yml`'s `tts-wrapper` service:

```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          device_ids: ['0']      # pin to GPU 0
          capabilities: [gpu]
```

Pin by GPU UUID (`nvidia-smi -L`) rather than index if PCIe enumeration is
unstable across reboots.
