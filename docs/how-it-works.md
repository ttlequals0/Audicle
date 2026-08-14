# How it works

The pipeline from a submitted URL (or uploaded document) to a finished episode.

```
        paywall bypass: a matched host's teaser triggers a re-scrape via
        Googlebot / Freedium / a custom proxy (or a clean fail);
        a detected Cloudflare challenge auto-routes through FlareSolverr
        |
        v
URL --> extract (direct / Firecrawl) --> cleanup (LLM)
                                              |
                                              v
                          normalize (LLM pronunciation pass +
                          base lexicon + regex corrections)
                                              |
                                              v
                              summary (LLM episode description)
                                              |
                                              v
                                   chunk + TTS (Chatterbox)
                                              |
                                              v
                        quality gate: audio QA + optional
                        Whisper ASR verify --> regen on fail
                                              |
                                              v
                            audio (ffmpeg) + artwork + VTT
                                              |
                                              v
                               finalize (write DB + RSS)
```

## Extraction

The default `direct` engine fetches the page in-process and parses it with trafilatura. Set `EXTRACTION_ENGINE=firecrawl` to use a self-hosted Firecrawl instead. Either way, extraction is a cascade, not a single fetch: JS-rendered and bot-gated pages fall back through FlareSolverr, the render sidecar, and the web archive, and per-host rules can route a site through a specific bypass. The whole cascade is covered in [Paywalled articles](paywalls.md).

Uploads skip extraction: a PDF, DOCX, Markdown, text, or HTML file is read directly, and a scan or image goes through on-device OCR (RapidOCR on CPU, models shipped in the image). A text PDF never pays the OCR cost, and a scan too blurry to read fails the job with a clear error instead of narrating noise. The `OCR_*` knobs (page cap, DPI, confidence floor, language) are in Settings under Uploads.

## Cleanup, normalize, summary

Three LLM passes. Cleanup strips the page down to the article. Normalize rewrites for narration: an LLM pronunciation pass plus the built-in base lexicon and your [pronunciation corrections](configuration.md#pronunciation-corrections). Summary writes the episode description that lands in the feed.

The pronunciation pass normally sends each whole chunk to the LLM. `PRONUNCIATION_SCOPE=sentence` sends only the sentences that matched a correction term, as numbered lines, and splices the respelled sentences back in place. That cuts tokens on long chunks with one correction in them. If the model breaks the numbered-reply format, the pass falls back to the whole-chunk call, and after the first such failure the job stops trying sentence scope at all, so a model that cannot follow the protocol costs one wasted call, not one per chunk.

The cleanup prompt is editable in Settings, and the [LLM provider](llm-providers.md) behind all three passes is switchable at runtime.

## Chunking and TTS

The normalized text is split into chunks sized for the TTS model (`TTS_CHUNK_*` settings). The chunker self-heals before TTS: it splits run-on sentences that arrive glued together (`end.Next`), and when a long sentence has no comma or semicolon to break on it falls back to a whitespace split instead of failing the job. Only a single word longer than the character cap is unsplittable.

Each chunk goes to the TTS wrapper, which narrates it by conditioning on your [reference voice](voices-and-tts.md). The wrapper can also be an OpenAI-compatible server on another host; see [remote backends](voices-and-tts.md#running-tts-on-another-host).

### The chunk cache

A chunk that clears the quality gates is also stored under `data/tts_cache`, keyed on everything that determines the audio: backend, model, voice, language, text, and the generation parameters. A re-run of a job that died partway, or a reprocess with unchanged settings, serves those chunks from disk instead of paying for synthesis again. The gates stay honest across settings changes: an entry stored while verification was off is not served once verification is on, and an entry that never went through the gates (both disabled at the time) is not served once either gate is enabled. Seed 0 means "random take every run", so nothing is cached in that mode. `TTS_CHUNK_CACHE_ENABLED` turns it off; the daily sweep prunes entries older than `TTS_CACHE_RETENTION_DAYS` (default 7). Budget roughly 170 MB of disk per hour of narrated audio for the retention window.

### Adaptive chunk sizing

Some articles fight the model: dense punctuation, code, unusual names. When 2 of a job's first 20 chunks need a regeneration, the rest of that job synthesizes at 75% of `CHATTERBOX_MAX_CHARS`, which is cheaper than paying a regen on most chunks. The reduction is per job, never raised back mid-job, and `TTS_ADAPTIVE_MAX_CHARS_ENABLED` turns it off.

## The quality gates

Every chunk passes a gate before the audio stage.

The wrapper trims each generated piece's edge silence at the source, because Chatterbox sometimes fails to stop after the text completes and pads to its 40 s generation cap with dead air. Trimming there means the gate, the ASR check, and the published audio all see speech rather than padding.

**Audio analysis** is signal-level: it catches a take that came back as a flat drone, steady noise, dead air, or a pitch that drifted away from the episode's running median. A bad take is regenerated, and each retry differs from the last by more than a fresh seed: it also shortens the text sent per model call and raises the repetition penalty, because re-rolling the seed alone tends to land in the same failure. The thresholds and how hard each retry escalates are live-tunable from the Audio analysis group in Settings.

**ASR verification** is an optional second check. With it on, the wrapper transcribes each chunk with faster-whisper and the backend compares that transcript to the text it asked for. Two signals come out of one comparison: overall word-level divergence (dropped content, a hallucinated run, a leaked preamble) and the longest contiguous stretch of diverging words, which catches a few seconds of garbled audio inside an otherwise fine chunk. Either one over its limit regenerates the chunk. The transcription is blind (the expected text is never fed to Whisper as a prompt), so the comparison stays honest. A remote ASR backend that is down normally degrades to shipping the chunk unverified; `WHISPER_API_STRICT` flips that to failing the chunk instead, for operators who want "verified or not shipped". Setup and tuning are in [Voices and TTS](voices-and-tts.md#asr-verification).

## The wrapper's memory ladder

The TTS wrapper serves hundreds of chunks from one long-lived process, and resident memory grows as it does. Left alone, that growth once reached the point where the host's OOM killer took the process out mid-chunk, destroying jobs that had already run for over an hour.

The wrapper now watches its own resident size after every chunk and logs it (`rss_mb` on each `tts_chunk_done` event, so the growth curve is visible in the logs rather than reconstructed from a kernel OOM report):

| Stage | Trigger | Action |
|---|---|---|
| Measure | every chunk | log RSS |
| Soft clean | RSS above `TTS_MEMORY_SOFT_LIMIT_MB` (default 8000) | gc, CUDA cache flush, `malloc_trim`; logs what it reclaimed |
| Controlled restart | still above `TTS_MEMORY_HARD_LIMIT_MB` (default 14000) after cleaning | finish the in-flight response, then restart between chunks |
| Idle restart | RSS above the soft limit while the job queue is empty | the worker asks the wrapper to restart via `POST /maintenance/restart`, so the reload happens between jobs instead of mid-episode |

The restart is the part that protects jobs: a restart the wrapper chooses happens at a chunk boundary, so the client gets the chunk it asked for and only the next request meets a reloading wrapper. The backend rides that out with a connection-retry budget (`TTS_CONNECT_RETRY_MAX_SECONDS`, default 180 s) sized to outlast the wrapper's cold start, which takes 60 to 99 seconds while the models reload. Set either limit to 0 to disable that stage.

The idle restart moves that cost off the clock entirely when it can. After each finished job, if the queue is empty, the worker checks the wrapper's `/health` (which now reports `rss_mb` and `restart_recommended`) and asks a wrapper over its soft limit to restart right then, while nobody is waiting. The wrapper refuses with a 503 if an inference is still running. `TTS_IDLE_RESTART_ENABLED` turns the behavior off, and it never applies to the remote TTS backend.

## Audio, artwork, transcript, finalize

The chunk WAVs are streamed into one file with soundfile (never the whole episode in memory at once), then ffmpeg handles loudness normalization and the MP3 encode (silence handling and encode settings are tunable under Audio output in Settings). Artwork is fetched from the article or falls back to the show image, and gets embedded in the MP3 as well as served in the feed. The WebVTT transcript is built from the chunk timeline; [chapters](feeds-and-podcasting.md#chapters) come from the same timeline. Finalize writes the episode row and the feed picks it up.

The narration text is dumped to `media/{episode_id}.narration.txt` before synthesis starts, so a job that dies in TTS leaves the text that broke it behind. The orphan sweep reclaims it once no episode points at it.

## The job queue

There is no message queue. SQLite handles the work queue with a single locked row update, and the pipeline runs in a worker process separate from the web server. Fine for one or two operators, not the right shape for fanning out across hosts.

A job is killed for stalling, not for taking a long time. Finishing a stage or a chunk resets a `JOB_STALL_SECONDS` window; only silence for that long ends the job. An absolute ceiling of `max(JOB_TIMEOUT_SECONDS, chunks x JOB_TIMEOUT_PER_CHUNK_SECONDS) x JOB_TIMEOUT_CEILING_MULTIPLIER` still applies, so a job that inches forward forever cannot hold the worker. The failed job's error says which one fired. All four are live-tunable from the Job timeouts group in Settings.

[< Docs index](README.md)
