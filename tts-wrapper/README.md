# Audicle TTS Wrapper

FastAPI service wrapping [Coqui XTTS-v2](https://huggingface.co/coqui/XTTS-v2)
for narration synthesis. Audicle's main app POSTs cleaned text chunks here and
gets back WAV file paths on a shared volume.

## License notes

- **Code**: Audicle ships this wrapper under MPL 2.0, matching the upstream
  Idiap fork of Coqui TTS (`coqui-tts` on PyPI).
- **Model weights**: XTTS-v2 is licensed under the
  [Coqui Public Model License 1.0.0 (CPML)](https://coqui.ai/cpml.txt),
  which restricts use to non-commercial purposes. Coqui AI shut down in
  January 2024 and the paid commercial-license tier is no longer offered.
  Audicle does **not** redistribute the weights; the wrapper downloads them
  from Hugging Face on first run.

For personal self-hosted use the non-commercial restriction is fine. Operators
deploying Audicle in a commercial setting are responsible for sourcing a
licensed replacement model.

## Endpoints

| Method | Path        | Purpose                                                                |
|--------|-------------|------------------------------------------------------------------------|
| POST   | /generate   | Synthesize a chunk. Body: `{text, episode_id, chunk_index}`.           |
| GET    | /health     | `{ok, model_loaded, reference_loaded}`. 503 until everything is ready. |
| POST   | /reload     | Re-read `reference/voice.wav` and recompute speaker embeddings.        |

## Reference voice

Drop a single WAV at `backend/app/reference/voice.wav` on the host. The
compose mount makes it visible inside the container at
`/app/app/reference/voice.wav`. Spec (see `backend/app/reference/README.md` for
the authoritative version):

- 8-12 seconds recommended; 3-60 s hard limits enforced by `/api/v1/reference/commit`
- 24 kHz recommended (16-48 kHz accepted)
- Mono
- <= 5 MB
- Clean speech (no background music, low noise)

LibriTTS clips work well. The four files in this repo's `ref_audio/` are
LibriTTS-derived; convert one with ffmpeg if you don't have a preferred clip:

```
ffmpeg -i ref_audio/422-122949-0019.flac -ar 22050 -ac 1 -t 10 backend/app/reference/voice.wav
```

If `voice.wav` is missing or unreadable, the container exits non-zero at
startup so the restart loop surfaces the misconfig instead of serving 500s
forever.

## Local dev

CUDA hosts: `docker compose build tts-wrapper && docker compose up tts-wrapper`.

CPU-only hosts (5-10x slower):

```
docker build -t audicle-tts:cpu -f tts-wrapper/Dockerfile.cpu tts-wrapper/
```

Then set `TTS_DEVICE=cpu` in `.env` and pin the image name in
`docker-compose.yml` (or run the container manually).

## HF cache

The XTTS-v2 weights (~2 GB) download on first run and persist in the
`hf_cache` named volume so subsequent restarts load from disk instantly.

## Tunable generation params

`XTTS_TEMPERATURE`, `XTTS_LENGTH_PENALTY`, `XTTS_REPETITION_PENALTY`,
`XTTS_TOP_K`, `XTTS_TOP_P`. Defaults from build-plan.md.

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
