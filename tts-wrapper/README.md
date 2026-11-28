# Audicle TTS Wrapper

FastAPI service wrapping [Chatterbox](https://github.com/resemble-ai/chatterbox)
(Resemble AI's zero-shot voice cloning model) for narration synthesis. Audicle's
main app POSTs cleaned text chunks here and gets back WAV file paths on a shared
volume.

## License notes

- **Code**: Audicle ships this wrapper under MIT, matching the root application.
- **Model weights**: Chatterbox is MIT-licensed. The wrapper downloads the
  weights from Hugging Face on first run; Audicle does not redistribute them.
- **Watermark**: every output carries Resemble's inaudible PerTh watermark.
  The library has no flag to disable it.

Personal self-hosted use is fine. Check the model license before redistributing
generated audio commercially.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | /generate | Synthesize a chunk. Body: `text` (1-4000 chars), `episode_id`, `chunk_index`, plus optional `slot` (1-5), `language`, `verify` and the generation knobs below. Returns `{wav_path, duration_secs, sample_rate, transcript, inference_ms, verify_ms, rss_mb}`. |
| GET | /health/live | Liveness: `{ok, model_loaded}`. 503 until the model loads; ignores the reference voice. The compose healthcheck hits this. |
| GET | /health | Readiness: `ok`, `model_loaded`, `reference_loaded`, version, device, engine, Whisper state, `rss_mb` and `restart_recommended`. 503 until a model and a voice are loaded. |
| POST | /select-voice | Switch the active voice to a slot. Body: `{slot}` (1-5). 404 if the slot is empty. |
| POST | /reload | Re-encode the resting voice (the lowest filled slot) into the speaker conditionals. |
| GET | /models | List the available TTS models and their languages. |
| POST | /select-model | Swap the loaded model. Body: `{model}` (`chatterbox` or `chatterbox-multilingual`). |
| POST | /transcribe | Transcribe a WAV under `{data_dir}/media` with faster-whisper. Body: `{wav_path}`. 503 when Whisper is disabled. |
| GET, PUT | /runtime-config | Read or replace the runtime overrides for the memory, Whisper, timeout and logging settings. The app syncs these from Settings. |
| POST | /maintenance/restart | Restart the process between jobs. 503 while an inference is running. |

## Reference voices

Voices live in five slots; there is no separate `voice.wav`. The wrapper
conditions on `reference/voices/slot{1..5}.wav`, mounted from the host. It boots on its
lowest filled slot and switches per job via `/select-voice`. Manage slots through the
app's Settings UI or `POST /api/v1/reference/slots/{n}`; see
`backend/app/reference/README.md` for the clip spec and the authoritative version.

The wrapper starts without a voice: the model loads, `/health` reports
`reference_loaded=false`, and `/generate` returns 503 until a slot is uploaded. The
first job's `/select-voice` (or a `/reload`) then encodes it with no restart. Only a
model-load failure exits the process.

## Network

The wrapper listens on port 8000, dual-stack (IPv6 plus IPv4) when the container
can bind `::`, and IPv4 only (`0.0.0.0`) otherwise. `listen.py` opens the socket
at start, logs the address, and hands it to uvicorn.

## Local dev

CUDA hosts (compose pulls the published image and has no `build:` stanza, so build directly):

```
docker build -t audicle-tts:dev -f tts-wrapper/Dockerfile tts-wrapper/
```

Then pin that tag in `docker-compose.yml` and `docker compose up tts-wrapper`.
The GPU image is amd64-only and uses CUDA 12.6 wheels with Python 3.14. The host
driver must expose CUDA 12.6 or newer, or the container refuses to start. Use the
CPU image when that requirement cannot be met.

CPU-only hosts (5-10x slower):

```
docker build -t audicle-tts:cpu -f tts-wrapper/Dockerfile.cpu tts-wrapper/
```

Or skip the build and add `docker-compose.cpu.yml` to the compose command; it uses
the published `-cpu` image, sets `TTS_DEVICE=cpu` and drops the GPU reservation.
For a local build, set `TTS_DEVICE=cpu` in `.env` and pin the image name.

## Model cache

The Chatterbox weights download on first run. `HF_HOME` and `TTS_HOME` default
to `/data/hf_cache` and `/data/tts_home`, so the weights persist on the mounted
`/data` volume and subsequent restarts load from disk instantly. These paths are
writable by the non-root container user (uid 1000). Throwaway compile caches (`NUMBA_CACHE_DIR`, `MPLCONFIGDIR`,
`XDG_CACHE_HOME`, `HOME`) point at `/tmp`.

## Tunable generation params

The generation knobs are not env vars. The backend sends them on every
`/generate` request, so they are tuned live in the app's Settings page (or
`PUT /api/v1/settings`) with no wrapper restart. A request that omits a field
falls back to these defaults; out-of-range values return 422.

| Request field | Default | Allowed | Effect |
|---|---|---|---|
| `temperature` | 0.5 | >0 to 2.0 | Sampling temperature (down from Turbo's 0.8 to steady pronunciation). |
| `repetition_penalty` | 1.2 | 1.0-2.0 | Raise if words repeat or loop. |
| `top_p` | 0.95 | >0 to 1.0 | Nucleus sampling cap; lower = safer, less varied. |
| `top_k` | 1000 | 1-10000 | Top-k sampling cap; lower = safer, less varied. |
| `seed` | 1234 | 0 or more | Makes a chunk reproducible. 0 disables seeding. |
| `max_chars` | 300 | 100-2000 | Per-piece char cap; the wrapper splits each chunk into pieces under this and concatenates the audio. |

Exaggeration and CFG weight are not exposed: the Turbo model ignores both.
Exaggeration is pinned to 0.0 (neutral read) when the reference voice is encoded.

Structural env vars (`TTS_DEVICE`, `TTS_LANGUAGE`, `TTS_SAMPLE_RATE`, the
`WHISPER_*` verification settings, `TTS_MEMORY_SOFT_LIMIT_MB` and
`TTS_MEMORY_HARD_LIMIT_MB`, `TTS_IDLE_UNLOAD_SECONDS`) are listed with defaults in
[TTS wrapper settings](../docs/environment-variables.md#tts-wrapper-settings). The
memory limits are explained in
[the memory ladder](../docs/how-it-works.md#the-wrappers-memory-ladder).

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
