# How it works

The pipeline from a submitted URL (or uploaded document) to a finished episode.

```
        paywall bypass: a short scrape escalates through the host's strategy
        (Googlebot by default), FlareSolverr, the reader proxy, wallhop, then
        an archive (or a clean fail); a Cloudflare challenge goes to
        FlareSolverr first
        |
        v
URL --> extract (direct / Firecrawl) --> cleanup (LLM)
                                              |
                                              v
                     title/author intro line + summary (LLM)
                                              |
                                              v
                                      chunk text
                                              |
                                              v
                          normalize pronunciation per chunk
                          (LLM + base lexicon + corrections)
                                              |
                                              v
                                    TTS (Chatterbox)
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

The default `direct` engine fetches the page in-process and parses it with trafilatura. Set `EXTRACTION_ENGINE=firecrawl` to use a self-hosted Firecrawl instead. Either way, a failed or short fetch escalates through a cascade of fallbacks. JS-rendered and bot-gated pages fall back through Googlebot, FlareSolverr, the reader proxy, wallhop, the render sidecar, and the web archive. Per-host rules can route a site through a specific bypass. The whole cascade is covered in [Paywalled articles](paywalls.md).

Uploads skip the web-fetch cascade. DOCX paragraphs and tables are read in document order. PDF pages with usable text are read directly; scanned pages use on-device OCR (RapidOCR on CPU, models shipped in the image). Markdown, text, and HTML are read directly; image uploads use OCR. EPUB books are read in spine order. The `OCR_*` knobs (page cap, DPI, confidence floor, language) are in Settings under Uploads.

### Books and chapters

With "Split into chapters" on, an EPUB or PDF becomes a book with one episode per chapter instead of one long job. A failure late in a long book then costs one chapter instead of hours of narration. Each chapter is a normal job with its own episode, so the pipeline, timeouts, retry, cancel and reprocess work unchanged.

- **Where chapters come from.** For an EPUB, the table of contents: the EPUB 3 nav document, else the EPUB 2 `toc.ncx`, else one chapter per spine document. For a PDF, the bookmark outline. Only top-level entries count, and a chapter's text runs to the next top-level entry, so an entry such as "Notes" that spans many files stays one chapter. Audicle does not guess chapters from headings. A file with no usable chapters is queued as one episode. A PDF needs at least two usable outline items and an EPUB at least two non-empty chapters.
- **Defaults.** Front and back matter (cover, title page, copyright, contents, dedication, epigraph, notes, index, bibliography, acknowledgments, about the author and similar) is unticked by default. A chapter shorter than `MIN_EXTRACTION_CHARS` cannot be selected, unless it is a PDF chapter with images OCR can read.
- **Limits.** An EPUB is rejected if its archive exceeds 200 MiB uncompressed or 5000 entries, or if it holds more than 50 MiB of text (fixed limits, not settings). An EPUB whose spine documents are encrypted is rejected as DRM protected; font-only obfuscation is ignored.
- **Statuses.** A chapter is `not_queued`, `queued`, `processing`, `done`, `failed`, `cancelled` or `removed`. The status is derived on each read from the chapter's newest job and its episode.
- **Resume.** Resume queues the chapters you requested earlier that are not done and not running, so a failed chapter runs again and finished ones are not repeated. Finished audio chunks are reused through the [chunk cache](#the-chunk-cache) for `TTS_CACHE_RETENTION_DAYS`; after that, the one failed chapter is synthesized from the start.
- **One voice.** A book narrates in one voice, stored when chapters are first queued.
- **Storage.** The original file and cover live in `{DATA_DIR}/books/{book_id}/`, outside the media directory, so the orphan sweep never removes them. They stay until the book is deleted. Deleting the book keeps published chapter episodes in the feed, but they can no longer be reprocessed. Uploading the same file again picks those episodes up: they show as done and move to the new season.
- **Cover.** A chapter episode uses the book's cover as its artwork. The cover is scaled to fit a square on a background of its average colour instead of being cropped. A small cover is used rather than falling back to the show image.
- **Retention.** Chapter episodes follow `RETENTION_DAYS`. An expired or deleted chapter shows as `removed`, and Resume does not narrate it again.

The feed tags chapter episodes with season and episode numbers; see [Feeds > Books and seasons](feeds-and-podcasting.md#books-and-seasons). The queue claims jobs created in the same second in insertion order, so chapters queued together run in chapter order.

## Cleanup, summary, chunking, and pronunciation

Cleanup strips the page down to the article. The pipeline then prepends a "Title. By Author." line (`INTRO_READ_ENABLED`), has the LLM write the episode summary, and splits the cleaned text into TTS chunks. A chunk that contains a term from the base lexicon or your [pronunciation corrections](configuration.md#pronunciation-corrections) goes through the LLM pronunciation pass with only the matching terms in the prompt; a chunk with no match skips the LLM. The deterministic corrections then run over every chunk before synthesis. The stored cleaned transcript stays separate from the pronunciation-adjusted speech text.

The pronunciation pass normally sends each whole chunk to the LLM. `PRONUNCIATION_SCOPE=sentence` sends only the sentences that matched a correction term, as numbered lines, and splices the respelled sentences back in place. That cuts tokens on long chunks with one correction in them. If the model breaks the numbered-reply format, the pass falls back to the whole-chunk call. After the first such failure the job stops trying sentence scope, so a model that cannot follow the protocol costs one wasted call, not one per chunk.

The cleanup prompt is editable in Settings, and the [LLM provider](llm-providers.md) behind all three passes is switchable at runtime.

## Chunking and TTS

The normalized text is split into chunks sized for the TTS model (`TTS_CHUNK_*` settings). The chunker self-heals before TTS. It splits run-on sentences that arrive glued together (`end.Next`). When a long sentence has no comma or semicolon to break on, it falls back to a whitespace split instead of failing the job. Only a single word longer than the character cap is unsplittable.

Each chunk goes to the TTS wrapper, which narrates it by conditioning on your [reference voice](voices-and-tts.md). The wrapper can also be an OpenAI-compatible server on another host; see [remote backends](voices-and-tts.md#running-tts-on-another-host).

### The chunk cache

A chunk that clears the quality gates is also stored under `data/tts_cache`, keyed on everything that determines the audio: backend, model, voice, language, text, and the generation parameters. A re-run of a job that died partway, or a reprocess with unchanged settings, serves those chunks from disk instead of paying for synthesis again. A hit skips synthesis only. The current quality gates still run on it, and with ASR verification on, the cached audio is transcribed again. On the wrapper backend, nothing is cached when `CHATTERBOX_SEED` is 0 (random take every run) or when no model is selected. `TTS_CHUNK_CACHE_ENABLED` turns the cache off. The daily sweep prunes entries older than `TTS_CACHE_RETENTION_DAYS` (default 7). Budget roughly 170 MB of disk per hour of narrated audio for the retention window (24 kHz, 16-bit mono WAV is 172.8 MB per hour).

### Adaptive chunk sizing

Some articles fight the model: dense punctuation, code, unusual names. When 2 of a job's first 20 chunks need a regeneration, the rest of that job synthesizes at 75% of `CHATTERBOX_MAX_CHARS`. That is cheaper than paying a regen on most chunks. The reduction is per job, never raised back mid-job, and `TTS_ADAPTIVE_MAX_CHARS_ENABLED` turns it off.

## The quality gates

Every chunk passes a gate before the audio stage. A chunk that still fails after `AUDIO_ANALYSIS_MAX_REGEN` regenerations (default 2) does not fail the job: the last take is kept (or, for a pitch-only failure, the take closest to the reference pitch) and a warning is logged.

The wrapper trims each generated piece's edge silence at the source. Chatterbox sometimes fails to stop after the text completes and pads to its 40 s generation cap with dead air. Trimming there means the gate, the ASR check, and the published audio all see speech rather than padding.

**Audio analysis** works at the signal level: it catches a take that came back as a flat drone, steady noise, dead air, or a pitch that drifted away from the episode's running median. A bad take is regenerated, and each retry differs from the last by more than a fresh seed. It also shortens the text sent per model call and raises the repetition penalty, because re-rolling the seed alone tends to land in the same failure. The thresholds and how hard each retry escalates are tunable live from the Audio analysis group in Settings.

**ASR verification** is an optional second check. With it on, the wrapper transcribes each chunk with faster-whisper and the backend compares that transcript to the text it asked for. Two signals come out of one comparison: overall word-level divergence (dropped content, a hallucinated run, a leaked preamble) and the longest contiguous stretch of diverging words. The second catches a few seconds of garbled audio inside an otherwise fine chunk. Either one over its limit regenerates the chunk. The transcription is blind (the expected text is never fed to Whisper as a prompt). A remote ASR backend that is down normally degrades to shipping the chunk unverified; `WHISPER_API_STRICT` flips that to failing the chunk instead, for operators who want "verified or not shipped". Setup and tuning are in [Voices and TTS](voices-and-tts.md#asr-verification).

## The wrapper's memory ladder

The TTS wrapper serves hundreds of chunks from one long-lived process, and resident memory grows as it does. Left alone, the host's OOM killer can take the process out mid-chunk and destroy a long job.

The wrapper checks its resident size after every chunk and logs it as `rss_mb` on each `tts_chunk_done` event, so the growth curve is visible in the logs:

| Stage | Trigger | Action |
|---|---|---|
| Measure | every chunk | log RSS |
| Soft clean | RSS above `TTS_MEMORY_SOFT_LIMIT_MB` (default 8000) | gc, CUDA cache flush, `malloc_trim`; logs what it reclaimed |
| Controlled restart | still above `TTS_MEMORY_HARD_LIMIT_MB` (default 14000) after cleaning | finish the in-flight response, then restart between chunks |
| Idle restart | RSS above the soft limit while the job queue is empty | the worker asks the wrapper to restart via `POST /maintenance/restart`, so the reload happens between jobs instead of mid-episode |

A restart the wrapper chooses happens at a chunk boundary, so the client gets the chunk it asked for and only the next request meets a reloading wrapper. The backend rides that out with a connection-retry budget (`TTS_CONNECT_RETRY_MAX_SECONDS`, default 180 s), sized to outlast the wrapper's cold start of 60 to 99 seconds while the models reload. Set either limit to 0 to disable that stage.

After each finished job, if the queue is empty, the worker checks the wrapper's `/health`, which reports `rss_mb` and `restart_recommended`, and asks a wrapper over its soft limit to restart while nobody is waiting. The wrapper refuses with a 503 if an inference is still running. `TTS_IDLE_RESTART_ENABLED` turns this off, and it never applies to the remote TTS backend.

## Audio, artwork, transcript, finalize

The chunk WAVs are streamed into one file with soundfile (never the whole episode in memory at once). ffmpeg then normalizes loudness and encodes the MP3; silence handling and encode settings are tunable under Audio output in Settings. Artwork is fetched from the article or falls back to the show image, and gets embedded in the MP3 as well as served in the feed. The WebVTT transcript is built from the chunk timeline; [chapters](feeds-and-podcasting.md#chapters) come from the same timeline. Finalize writes the episode row and the feed picks it up.

The narration text is dumped to `media/{episode_id}.narration.txt` before synthesis starts, so a job that dies in TTS leaves the text that broke it behind. The orphan sweep reclaims it once no episode points at it.

## The job queue

There is no message queue. SQLite handles the work queue with a single locked row update, and the pipeline runs in a worker process separate from the web server. That fits one or two operators but not fanning out across hosts.

A job is killed for stalling, not for taking a long time. Finishing a stage or a chunk resets a `JOB_STALL_SECONDS` window; only silence for that long ends the job. An absolute ceiling of `max(JOB_TIMEOUT_SECONDS, chunks x JOB_TIMEOUT_PER_CHUNK_SECONDS) x JOB_TIMEOUT_CEILING_MULTIPLIER` still applies, so a job that inches forward forever cannot hold the worker. The failed job's error says which one fired. All four are tunable live from the Job timeouts group in Settings.

[< Docs index](README.md)
