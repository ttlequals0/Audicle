# Glossary

Every term the app uses, in plain words, with a link to the part of the docs that covers it. If you hit a word in the UI that is not here, open an issue.

## A

**ASR verification** - The optional quality gate that transcribes each generated chunk with Whisper and compares the transcript to the requested text; a chunk that diverges too far is regenerated. [Voices and TTS > ASR verification](voices-and-tts.md#asr-verification)

**Audio analysis** - The signal-level quality gate over every chunk: it catches drones, noise, dead air, pacing problems, and pitch drift, and triggers a regeneration. [How it works > The quality gates](how-it-works.md#the-quality-gates)

**Audition** - Playing a short TTS sample of a voice slot from Settings before using it for an episode. [Voices and TTS](voices-and-tts.md#reference-voice-slots)

**Authenticated feeds** - An optional 64-hex key on every feed and media URL so only clients holding it can read your feed. [Feeds > Authenticated feeds](feeds-and-podcasting.md#authenticated-feeds)

## C

**Chapters** - Marker points with titles in an episode, picked by one LLM call and shipped both as Podcasting 2.0 JSON and as ID3 frames in the MP3. [Feeds > Chapters](feeds-and-podcasting.md#chapters)

**Chime** - An optional short clip played at the end of every episode so back-to-back episodes are easy to tell apart. [Voices and TTS > End-of-episode chime](voices-and-tts.md#end-of-episode-chime)

**Chunk** - One piece of the narration text, sized for a single TTS call. Episodes are synthesized chunk by chunk, and the quality gates judge each one. [How it works > Chunking and TTS](how-it-works.md#chunking-and-tts)

**Cleanup** - The LLM pass that strips an extracted page down to the article. Its prompt is editable. [How it works](how-it-works.md#cleanup-normalize-summary)

**Convenience mode** - The state before an admin password is set: every admin endpoint is open. Fine on a private network, warned about loudly otherwise. [Installation](installation.md#required-configuration)

**Cookie jar** - Your logged-in session cookies for a subscribed site, stored against its FlareSolverr rule so articles fetch as you. [Paywalls > Subscriber paywalls](paywalls.md#subscriber-paywalls-cookie-jar)

**Correction** - A row in the pronunciation table: a match term and the spoken form the narrator should say instead. [Configuration > Pronunciation corrections](configuration.md#pronunciation-corrections)

## E

**Extraction** - Turning a URL into article text. A cascade of engines and fallbacks, not a single fetch. [How it works > Extraction](how-it-works.md#extraction)

**Extraction floor** - `MIN_EXTRACTION_CHARS`: a scrape below it counts as blocked and triggers the bypass cascade. [Paywalls](paywalls.md)

## F

**Feed key** - The secret in authenticated feed URLs. Regenerating it breaks every existing subscription on purpose. [Feeds > Authenticated feeds](feeds-and-podcasting.md#authenticated-feeds)

**FlareSolverr** - A self-hosted real-browser proxy Audicle can route hard-blocked hosts through. Not bundled. [Paywalls > Hard blocks](paywalls.md#hard-blocks)

## H

**Hard block** - A site that answers a scrape with a challenge page or near-empty 403 rather than a teaser. Detected and routed through the solver automatically. [Paywalls > Hard blocks](paywalls.md#hard-blocks)

## J

**Job** - One run of the pipeline for one submission. Queued, processing, done, failed, or cancelled; visible on Home. [How it works > The job queue](how-it-works.md#the-job-queue)

**Job timeouts** - The watchdog policy: a job dies for stalling (no progress for `JOB_STALL_SECONDS`), with an absolute ceiling on top. [How it works > The job queue](how-it-works.md#the-job-queue)

## M

**Memory ladder** - The wrapper's self-defence against unbounded memory growth: measure RSS per chunk, clean above a soft limit, restart between chunks above a hard limit. [How it works > The wrapper's memory ladder](how-it-works.md#the-wrappers-memory-ladder)

## N

**Normalize** - The pipeline stage that rewrites cleaned text for narration: an LLM pronunciation pass plus the base lexicon and your corrections. [How it works](how-it-works.md#cleanup-normalize-summary)

## O

**OCR** - On-device text recognition for scanned PDFs and image uploads (RapidOCR, CPU, models shipped in the image). [How it works > Extraction](how-it-works.md#extraction)

## R

**Reference voice** - The short clip a voice slot holds; the TTS model conditions on it to clone the voice. [Voices and TTS](voices-and-tts.md#reference-voice-slots)

**Registration wall** - A "give us an email to keep reading" gate. With `REGISTRATION_EMAIL` set, the render sidecar answers the form and re-reads the unlocked page. [Paywalls > Registration walls](paywalls.md#registration-walls)

**Render sidecar** - The bundled headful-browser container that clicks expand-to-continue gates and answers registration walls. Optional; the app tolerates it being down. [Paywalls](paywalls.md#the-strategies)

**Reprocess** - Re-running the whole pipeline for an existing episode. The episode's GUID revision bumps so subscribed clients re-download the new audio. [Web interface > Feed](web-interface.md#feed)

**Reset to defaults** - The per-card control on Settings that puts a card's fields back to the shipped defaults. Edits the form; nothing changes until you save. [Web interface > Settings](web-interface.md#settings)

**Retention** - The daily sweep that deletes episodes older than `RETENTION_DAYS`. [Feeds > Retention](feeds-and-podcasting.md#retention)

**Runtime setting** - A setting editable from the UI or API with no restart, stored in the database over the env value. [Configuration](configuration.md)

## S

**Site override** - A per-host paywall rule: which bypass strategy a host gets, its teaser threshold, and optionally a cookie jar. [Paywalls](paywalls.md)

**Slot** - One of the five labelled reference-voice positions. [Voices and TTS](voices-and-tts.md#reference-voice-slots)

**Summary** - The LLM-written episode description that lands in the feed. [How it works](how-it-works.md#cleanup-normalize-summary)

## T

**Teaser** - The free fragment a paywalled site serves a scraper. Detected by length (or JSON-LD `articleBody` for hosts with a rule) and routed to a bypass instead of narrated. [Paywalls > Teaser detection](paywalls.md#teaser-detection)

**TTS backend** - Where synthesis runs: the bundled wrapper, or any OpenAI-compatible speech server on another host. [Voices and TTS > Running TTS on another host](voices-and-tts.md#running-tts-on-another-host)

**TTS wrapper** - The separate GPU container running Chatterbox. It conditions on your reference voice, trims edge silence, optionally transcribes for verification, and watches its own memory. [Installation > Three containers](installation.md#three-containers)

## W

**Webhook** - The JSON POST Audicle sends your URL when an episode finishes or fails. [API and webhooks > Webhooks](api-and-webhooks.md#webhooks)

**Whisper backend** - Where ASR verification transcribes: the bundled wrapper, a remote OpenAI-compatible transcription server, or off. [Voices and TTS > ASR verification](voices-and-tts.md#asr-verification)

[< Docs index](README.md)
