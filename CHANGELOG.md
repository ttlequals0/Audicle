# Changelog

All notable changes to Audicle are recorded here. Format follows Keep a Changelog
(https://keepachangelog.com). Versioning is semver once a release ships; pre-release
work lives under `[Unreleased]`.

## [Unreleased]

### Added (Phase 4 - TTS Wrapper)

- `tts-wrapper/` — new sibling container that wraps Coqui XTTS-v2 in a FastAPI service. Endpoints per build plan:
  - `POST /generate` accepts `{text, episode_id, chunk_index}`, runs inference under an `asyncio.Lock` so concurrent calls queue at the GPU boundary while `/health` stays responsive, writes the result to `/data/media/{episode_id}_chunk_{chunk_index}.wav`, returns `{wav_path, duration_secs, sample_rate}`.
  - `GET /health` reports `{ok, model_loaded, reference_loaded}`; 503 until the model has loaded AND the reference embeddings are computed.
  - `POST /reload` acquires the same lock, re-reads `reference/voice.wav`, and recomputes speaker embeddings — called by the main app after a reference-voice commit (Phase 10).
- `tts-wrapper/engine.py`: `Engine` Protocol + `XTTSEngine` real implementation. The Coqui TTS and PyTorch imports are deferred to `XTTSEngine.load()` so the module is importable in test environments without GPU runtime. Tests inject a `FakeEngine` via `create_app(engine=…, data_dir=…)`.
- `tts-wrapper/main.py`: lifespan calls `engine.load()` and `sys.exit(1)`s on failure so uvicorn exits non-zero and the container restart policy fires (matches the missing-`voice.wav` and model-load-failure deliverables). Pydantic request model uses `extra="forbid"`, `min_length` on text + episode_id, `ge=0` on `chunk_index`. GPU OOM raises a typed `GPUOutOfMemoryError` that the route catches, calls `torch.cuda.empty_cache()`, and returns 500 with `{error: "GPU OOM", cause}`. WAV duration is computed from the file header so the response value matches what was written.
- `tts-wrapper/config.py`: env-driven `Config` carrying `TTS_DEVICE`, `TTS_LANGUAGE`, the XTTS generation tunables (`XTTS_TEMPERATURE`/`LENGTH_PENALTY`/`REPETITION_PENALTY`/`TOP_K`/`TOP_P`), and the sample rate. Defaults match build-plan.md.
- `tts-wrapper/Dockerfile`: `pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime` base, `coqui-tts` (idiap fork) from PyPI, `libsndfile1`, `curl` for the healthcheck. `uvicorn main:create_app --factory --workers 1 --timeout-keep-alive 300`. Healthcheck on `/health` with 120s `start_period` to cover the model download/load on cold start.
- `tts-wrapper/Dockerfile.cpu`: alternate base (`pytorch/pytorch:2.4.0-cpu`) for hosts without CUDA. 5-10× slower per build plan but verifies the contract.
- `tts-wrapper/README.md`: XTTS CPML license note, voice-file specs (6-12s, 22050+ Hz, mono, clean) with the exact ffmpeg conversion line for the LibriTTS clips in `ref_audio/`, HF cache mount, CPU vs CUDA, GPU pinning.
- `backend/app/services/tts.py`: async client for the wrapper. `generate_chunk(text, episode_id, chunk_index, settings)` returns a frozen `GenerateResult`. Typed exceptions `TTSTimeoutError`, `TTSProviderError` (5xx + network, retryable), `TTSRequestError` (4xx + malformed). `reload(settings)` posts `/reload` and returns the wrapper's body.
- `backend/app/services/reachability.py`: added `check_tts(settings)` with the build-plan grace-period semantics (poll up to `TTS_REACHABILITY_GRACE_SECONDS` with `TTS_REACHABILITY_PROBE_TIMEOUT` per attempt, return on first `model_loaded: true`). Wired into `run_all` so the worker waits for the wrapper before processing.
- `backend/app/config.py`: added `TTS_LANGUAGE`, `TTS_DEVICE`, `TTS_HTTP_TIMEOUT_SECONDS`, `TTS_RETRY_COUNT`, `TTS_REACHABILITY_GRACE_SECONDS`, `TTS_REACHABILITY_PROBE_TIMEOUT`, and the five `XTTS_*` tunables. `.env.example` documents every default.
- `docker-compose.yml`: added the `tts-wrapper` service with nvidia GPU reservation (`device_ids: ['0']`), shared `./data:/data`, read-only reference mount, and a named `hf_cache` volume so the ~2GB model download survives container rebuilds. `app` now `depends_on: tts-wrapper: condition: service_healthy`.

Tests (22 new, 153 total):

- `tts-wrapper/tests/test_main.py` (9): /health 200 path + 503 when reference not loaded; /generate writes the WAV at the expected `/data/media/{episode}_chunk_{n}.wav` path with the duration computed from the header; blank text rejected; negative chunk_index rejected; extra fields rejected; GPU OOM surfaces the 500 envelope with cause; sequential /generate calls observed in submission order via the lock; /reload returns `{ok: true, reference_loaded: true}` and increments the engine's reload counter.
- `backend/tests/test_tts.py` (9): client wire format (path + body), 5xx/4xx/timeout/network error classification, non-JSON + missing-key shape errors, /reload happy + 5xx paths.
- `backend/tests/test_tts_reachability.py` (4): probe succeeds on first try, polls until `model_loaded` flips to true, reports `grace period expired` on network failure, reports `model_loaded=false` when the wrapper is up but not ready.

Verification approach
- The wrapper engine abstraction means the FastAPI contract is exercised end-to-end in CI without ever loading XTTS-v2. The real model load + GPU inference is operator-side per the build plan deliverable ("Manual test: send chunk text, get WAV back; verify CPU fallback path"). The `tts-wrapper/README.md` documents the exact `docker compose build tts-wrapper && docker compose up` flow plus a single-curl smoke test once the wrapper reports `model_loaded: true`.
- `docker compose config` validates the full multi-service stack (app + tts-wrapper) including the GPU reservation, mount layout, env wiring, healthcheck dependency, and named volumes.

### Code-review pass (multi-agent /simplify + /code-review for Phase 4)

Findings surfaced and applied:

- **Path traversal blocked**: `GenerateRequest.episode_id` now carries a `pattern=r"^[A-Za-z0-9_.-]+$"` and `chunk_index` an upper bound. The wrapper also runs a `Path.resolve().is_relative_to(out_dir)` belt-and-braces check before writing, so an episode_id that somehow slips through the validator still can't escape `data_dir/media/`.
- **Event loop unblocked**: `XTTSEngine.synthesize` and `_compute_embeddings` now run via `asyncio.to_thread(...)`. The wrapper's `/health` route is genuinely responsive while a chunk is mid-inference, not just lock-free.
- **Per-request inference timeout**: `/generate` wraps `engine.synthesize` in `asyncio.wait_for(timeout=TTS_REQUEST_TIMEOUT_SECONDS)` (default 120s, env-tunable) and returns 504 on timeout. Build-plan line 822's "Per-request timeout 120s" is now enforced; a wedged inference call can no longer hold the worker forever.
- **WAV writes are atomic**: `_atomic_write_bytes` (tempfile + fsync + os.replace) replaces the bare `Path.write_bytes` so Phase 5+ stitching can't observe a partial chunk file.
- **Lifespan re-raises instead of `sys.exit(1)`**: the original exception now propagates through Starlette's documented `lifespan.startup.failed` path, giving uvicorn a clean Exception-based exit and making the failure path safely testable via `TestClient.__enter__`.
- **Reload no longer corrupts engine state on failure**: `reload_reference` snapshots the prior latent + speaker_embedding + reference_loaded flag and rolls them back if `_compute_embeddings` raises. A bad voice.wav now returns 500 but `/health` keeps reporting the prior good state instead of flipping to permanent `reference_loaded=false`.
- **Spec drift fixed on response bodies**: `/reload` now returns just `{"ok": true}` per build-plan line 805; `/health` 503 body now includes a diagnostic `"error"` string per build-plan line 803.
- **Coqui-tts version pinned**: `tts-wrapper/pyproject.toml` upper-bounds the dep to `<0.25` and a comment explains that the wrapper reaches into Coqui internals (`model.synthesizer.tts_model.get_conditioning_latents`/`inference`) not covered by semver.
- **Dockerfile installs from pyproject**: both `Dockerfile` and `Dockerfile.cpu` drop their hand-written `pip install` list in favor of `pip install --no-cache-dir .`. Single source of truth across the venv-based dev and the container build.
- **app.state.lock created at app construction**: no longer split between module-time engine assignment and lifespan-time lock assignment; consistent ordering for ASGI middleware or unusual test harnesses that bypass the lifespan.
- **Compose security hardening parity**: tts-wrapper service now declares `security_opt: no-new-privileges: true` and `cap_drop: ALL`, matching the app service posture.
- **check_tts AsyncClient pooled across probes**: one `httpx.AsyncClient` for the whole grace window so successive probes share the TCP connection pool. Per-probe debug log line emitted so a stalled cold-start shows up in Loki instead of 60 silent seconds.
- **Module-style imports moved to top in `engine.py`**: `io`, `wave`, and `numpy` (transitive numpy dep) no longer hide as inline imports inside `_wav_bytes`, matching the project's "no inline imports" rule.

New tests added by the review pass (5 more, 158 total):

- `tts-wrapper/tests/test_main.py`: path traversal in `episode_id` rejected (covers `../etc/foo`, `a/b`, `with space`, `with\x00null`); `/reload` 404 when engine raises `FileNotFoundError`; `/reload` 500 when engine raises any other exception (each path matters because the backend's `tts.reload` client classifies 4xx as terminal `TTSRequestError` and 5xx as retryable `TTSProviderError`); engine `sample_rate` mismatch is observable in the response (FakeEngine.sample_rate=24000 over a 22050 Hz WAV → response carries the engine's value, duration computed from the WAV header — a future code change that reads the rate from the header fails the assertion); atomic write leaves no `.tmp` files on success.

Container smoke remains operator-side per the original Phase 4 plan; the additional fixes don't change the manual XTTS smoke path documented in `tts-wrapper/README.md`.

### Added (Phase 3 - LLM Cleanup)

- `backend/app/services/llm.py`: multi-provider client. `async generate(system, user, settings, *, temperature?, max_tokens?)` dispatches to `_call_openai_compatible` (POST `{OPENAI_BASE_URL}/chat/completions`) or `_call_anthropic` (POST `https://api.anthropic.com/v1/messages` with `x-api-key` + `anthropic-version` headers). Typed errors: `LLMTimeoutError`, `LLMProviderError` (5xx, retryable), `LLMRequestError` (4xx + malformed JSON, non-retryable). Both providers return parsed text via the established response shapes.
- `backend/app/services/corrections.py`: single-pass alternation substitution. Whole-word matches via stricter lookarounds (`(?<![\w-])` / `(?![\w-])`) so `kubectl` doesn't match inside `kubectl-helper` AND keys ending in non-word symbols like `C++` still match next to whitespace. Case-sensitive, longest-key-first via regex alternation order, auto-escapes regex specials so operators can write `C++` or `node.js`. `validate(dictionary, max_entries)` returns a `ValidationResult` listing every failure (root-not-dict, entry-count-cap, per-key empty/length/whitespace/control-char) rather than raising. `load`/`save` round-trip the JSON file with atomic temp-and-replace.
- `backend/app/services/prompt.py`: `load(path)` reads the file, `save(path, content, *, max_bytes)` writes atomically with a byte-length cap (not character-length, so multi-byte UTF-8 is enforced correctly). `PromptTooLargeError` surfaced separately so the API can return 413.
- `backend/app/prompts/script.txt`: replaced the Phase 1 placeholder with a real cleanup prompt that captures the build plan's remove / replace / transform / normalize / leave-alone behavior, with explicit output-format instructions (plain text only, no preamble, blank lines between paragraphs).
- `backend/app/services/pipeline.py`: pipeline now runs `extract` → `cleanup` → `corrections`. Final status=done with stage=corrections for Phase 3. Cleanup stage re-reads the prompt file every call so operator edits take effect on the next job without a restart. `MIN_CLEANUP_CHARS` guard mirrors the extract-stage threshold check. Corrections stage logs `entries_loaded` and `delta_chars`.
- `backend/app/services/reachability.py`: added `check_llm(settings)`. For `openai-compatible`, probes `GET {OPENAI_BASE_URL}/models` (the well-known list-models endpoint every Ollama / vLLM / LM Studio / OpenAI-compatible server exposes). For `anthropic`, no cheap probe exists per the build plan, so the check only validates `ANTHROPIC_API_KEY` is present. Wired into `run_all` so the worker exits non-zero on first boot when the LLM is unreachable.
- `backend/app/api/v1/prompt.py`: `GET /api/v1/prompt` returns `{prompt}`; `PUT /api/v1/prompt` accepts `{prompt}` with `extra="forbid"` and validates byte-length against `MAX_PROMPT_LENGTH_BYTES`. 413 with `{max_bytes, actual_bytes}` details on oversize, 404 when the underlying file is missing.
- `backend/app/api/v1/corrections.py`: `GET /api/v1/corrections` returns the full dictionary; `PUT /api/v1/corrections` accepts the full dict, runs `corrections.validate`, returns 400 with a per-entry failure list, otherwise persists atomically.
- `backend/app/api/v1/router.py`: mounts the two new routers alongside `/submit` and `/status/{job_id}`.
- `backend/app/config.py`: added `MAX_PROMPT_LENGTH_BYTES` (default 10240) and `MAX_CORRECTIONS_ENTRIES` (default 500).
- `.env.example`: documented both new env vars.

Tests (50 new, 109 total):

- `test_llm.py` (10): openai-compatible chat-completions wire format (path + Authorization + messages + temperature + max_tokens), 5xx → `LLMProviderError`, 4xx → `LLMRequestError`, ReadTimeout → `LLMTimeoutError`, non-JSON body → request error, unexpected response shape → request error; anthropic wire format (host + `x-api-key` + `anthropic-version` + system + messages), missing key surfaces clearly, non-text content block rejected, unknown provider raises.
- `test_corrections.py` (22): whole-word with hyphen-aware boundary, case sensitivity, longest-first via alternation, auto-escape for `C++` / `node.js`, empty-dict + no-match short circuits; validator rejects non-dict root, too-many-entries, empty key, oversize key/value, leading/trailing whitespace, empty value, control characters; load/save round-trip + atomic write (no partial file on failure) + missing/empty file returns `{}` + non-object root rejected.
- `test_prompt.py` (5): load returns contents, save round-trip, oversize raises (byte length not char length so multi-byte UTF-8 trips the cap correctly), atomic no-partial-on-failure.
- `test_api_prompt_corrections.py` (8): GET/PUT round-trip for both endpoints with file restoration after, `extra="forbid"` rejection on prompt, 413 on oversize prompt with `{max_bytes, actual_bytes}` details, 400 on bad correction entry, 400 on entry-count exceeded.
- `test_llm_reachability.py` (5): openai-compatible 200/network-failure/5xx paths; anthropic skips the HTTP probe and only validates the key.
- `test_pipeline.py`/`test_worker.py` (updated): stub both `extraction.extract` and `llm.generate`, expect status=done with stage=corrections.

Container smoke verified end-to-end with a mock Firecrawl + mock OpenAI-compatible server: reachability passes both checks, submit returns 201, status progresses queued → done with stage=corrections in single-digit ms, structured logs include `reachability_check` (firecrawl + llm), `stage_start`/`stage_end` for extract/cleanup/corrections, `cleanup_complete` with `input_chars`/`output_chars`, `corrections_complete` with `entries_loaded`/`delta_chars`, and `pipeline_done`. Prompt and corrections endpoints round-trip via curl.

### Code-review pass (multi-agent /simplify + /code-review for Phase 3)

Findings surfaced and applied:

- **Cleanup stage now wraps `llm.generate` with tenacity retry** per build plan line 251 (`LLM_RETRY_COUNT` attempts, exponential backoff, retries `LLMProviderError`/`LLMTimeoutError`, never retries `LLMRequestError`). Previously the cleanup stage failed permanently on the first transient 5xx; `LLM_RETRY_COUNT` was config-defined but unused.
- **`CleanupTooShortError` introduced** as a dedicated exception (subclasses `Exception`, not `ValueError`) so future broad `except ValueError` calls can't accidentally swallow the min-chars guard.
- **openai-compatible `content: null` handled cleanly**: providers that emit `tool_calls` instead of text return `content=null`; the cleanup stage previously crashed with `TypeError: object of type 'NoneType' has no len()`. Now classified as `LLMRequestError` with a clear message.
- **Anthropic multi-block responses now read correctly**: search-and-concatenate all `text` blocks instead of crashing if the first block is `thinking` or `tool_use`. Multi-text-block responses (extended thinking with citations) join their text content per Anthropic's documented usage.
- **Anthropic response shape now also catches `AttributeError`**: a non-dict content block (string, null) used to escape the typed handler as an opaque exception.
- **corrections.load now drops invalid entries with a WARN log**: a hand-edited file with empty keys would otherwise produce a regex like `(?<![\w-])(?:|kubectl)(?![\w-])` whose empty alternative matches at every word boundary. Sanitization mirrors the PUT validator so the bind-mount edit path is as safe as the API.
- **api/v1/corrections request body widened from `dict[str, str]` to `dict[str, Any]`** so Pydantic doesn't short-circuit non-string values before `corrections.validate` runs. Clients now receive the typed per-key failure envelope instead of the generic "Validation failed".
- **PromptBody now validates `min_length=1` AND rejects whitespace-only**: an admin accidentally clearing the textarea no longer silently writes an empty cleanup prompt.
- **`PromptTooLargeError` reparented from `ValueError` to `Exception`** so a downstream broad `except ValueError` can't accidentally swallow the 413 signal.
- **`reachability` log records renamed `stage="startup"` to `phase="startup"`** so reachability events don't collide with the pipeline-stage Loki label dimension used by every other log line.
- **Shared `services/atomic_write.py` helper** that both `prompt.save` and `corrections.save` now delegate to. Adds a parent-directory `fsync` after `os.replace` so the rename is durable across kernel crashes (the previous implementations fsynced the file but not the directory).
- **corrections.py module docstring updated** to reflect the lookaround boundary (which excludes hyphens) instead of the obsolete `\b` description.

New tests added by the review pass (9 more, 118 total):

- `test_llm.py` (3 new): Anthropic URL path + version locked (`/v1/messages`, `2023-06-01`); openai-compatible `content=null` raises typed `LLMRequestError`; Anthropic multi-block response with thinking + tool_use interleaved returns just the concatenated text blocks.
- `test_pipeline.py` (3 new): `MIN_CLEANUP_CHARS` guard fires with `stage=cleanup` and a clear error; cleanup retries once on `LLMProviderError` and ends with `status=done`; cleanup does NOT retry on `LLMRequestError` (exactly one attempt, ends with `status=failed`).
- `test_api_prompt_corrections.py` (3 new): blank prompt rejected (both empty and whitespace-only); byte-boundary test at exactly `MAX_PROMPT_LENGTH_BYTES` succeeds + one byte over returns 413 (guards against `>` → `>=` off-by-one); non-string value in PUT corrections surfaces as the typed failure envelope instead of the generic "Validation failed".
- The existing persist-roundtrip tests now use a yield-style `_preserve_prompt_file` / `_preserve_corrections_file` fixture so the on-disk file is always restored in teardown, even if any assertion in the body fails. Previously a single flaky assert could leave the repo's `script.txt` or `pronunciation.json` polluted.

### Added (Phase 2 - Extraction)

- Runtime deps: `httpx>=0.27`, `tenacity>=9.0`. `httpx` moved out of `[dependency-groups].dev` into runtime since the Firecrawl client and the reachability prober both use it.
- `backend/app/services/extraction.py`: async Firecrawl client. POST `{FIRECRAWL_URL}/v1/scrape` with `{url, formats: ["markdown"]}`. Tenacity `AsyncRetrying` with `FIRECRAWL_RETRY_COUNT` attempts and exponential backoff seeded by `FIRECRAWL_BACKOFF_BASE_SECONDS`. Typed exceptions: `ExtractionError` (base), `ExtractionTransientError` (5xx / network / timeout, retryable), `ExtractionPermanentError` (4xx / malformed JSON / `success=false`), `ExtractionTooShortError` (below `MIN_EXTRACTION_CHARS`). Returns a frozen `ExtractionResult(markdown, metadata)`.
- `backend/app/services/jobs.py`: pure DB helpers. `compute_episode_id(url)` = MD5 truncated to 12 hex; `get_job`, `get_job_by_episode_id`, `episode_exists`, `create_job` (with `DuplicateSubmissionError` and reprocess-wipes-prior semantics), `claim_next_queued` (atomic SELECT+UPDATE under `BEGIN IMMEDIATE`), `set_stage`, `mark_done`, `mark_failed`, `job_as_dict`. Every UPDATE bumps `updated_at` explicitly per the build plan's application-managed timestamps contract.
- `backend/app/services/pipeline.py`: `process_job(job, settings)` orchestrator. Wraps the whole job under `asyncio.wait_for(JOB_TIMEOUT_SECONDS)`. Each stage writes its name to `jobs.stage` BEFORE running so the timeout path can report which stage was executing. Stage start/end/failure structured logs with `job_id` + `episode_id` + `stage` stamped via contextvars. Phase 2 wires only the `extract` stage; on success status=done with `stage=extract`. Phase 3+ will append cleanup and beyond.
- `backend/app/services/reachability.py`: `check_firecrawl(settings)` and `run_all(settings)`. Probes `GET {FIRECRAWL_URL}/v1/health`. The worker calls `run_all` at startup and exits non-zero on failure so the container restart loop surfaces a misconfigured stack instead of every job failing the same way mid-pipeline. The FastAPI lifespan deliberately does NOT call `run_all` so `/health/ready` and the admin API stay reachable for triage even when Firecrawl is down.
- `backend/app/worker.py`: polling loop now actually picks up queued jobs (`_pickup_once`), runs `pipeline.process_job`, and continues. Single in-flight. Reachability checks run before crash recovery and signal-handler install; failure causes `sys.exit(1)` so the supervisor cycles the container.
- `backend/app/api/errors.py`: global error envelope. All 4xx and 5xx return `{error, status, details?}` per build plan. RequestValidationError → 400 with field details. Unhandled exceptions → 500 with logged traceback but no client-side leakage.
- `backend/app/api/v1/` with `router.py`, `submit.py`, `status.py`. `POST /api/v1/submit` accepts `{url: AnyHttpUrl, reprocess?: bool}`, returns 201 `{job_id, episode_id, status, replaced_previous}`. 409 on duplicate (in-flight job OR existing episode without reprocess). 400 on invalid URL. `GET /api/v1/status/{job_id}` returns full job state or 404.
- `backend/app/main.py`: mounts the v1 router and registers the error handlers alongside the existing health router.
- `backend/app/config.py`: `JOB_TIMEOUT_SECONDS` widened from `int` to `float` so fractional values work in tests; production default unchanged at 1800.
### Code-review pass (multi-agent /simplify + /code-review)

Findings surfaced and applied:

- **jobs.create_job race + non-atomic DELETE**: wrapped the entire duplicate-check + delete + insert in `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK`. Two concurrent submits for the same URL can no longer both pass the in-flight check; a failure between the two DELETE statements in the reprocess path no longer leaves a half-wiped DB.
- **claim_next_queued ROLLBACK secondary-error mask**: switched to `contextlib.suppress(OperationalError)` around the rollback, matching the pattern already used in `database._apply_pending`.
- **extraction NoneType chain on `data: null`**: hardened `_parse_response` and the data/markdown/metadata extraction to handle `data=null`, non-dict bodies, and non-dict data with a typed `ExtractionPermanentError` instead of `AttributeError`.
- **pipeline error finalization can secondary-raise**: extracted `_finalize_failure` that wraps `_last_stage` and `_persist_failure` in their own try/except; a locked DB during the error handler no longer masks the original stage exception and the structured `pipeline_failed` / `pipeline_timeout` log lines always fire.
- **_run_stage missed CancelledError**: widened the except from `Exception` to `BaseException` so timeout-cancelled stages still emit the `stage_failed` log with duration_ms.
- **worker poll loop unsafe**: wrapped `_process_one` in try/except so a transient DB or OS error logs and backs off instead of killing the worker process.
- **SubmitRequest silently dropped unknown fields**: added `model_config = ConfigDict(extra="forbid")` so `{"reprcess": true}` (typo) surfaces as a 400 Validation failed instead of being silently ignored.
- **AnyHttpUrl normalization changed user URL**: replaced the `AnyHttpUrl` field type with a `str` + `field_validator` that runs `AnyHttpUrl(value)` for validation but returns the raw string, keeping `episode_id` deterministic against the user-submitted URL and the persisted `url` byte-for-byte identical.
- **_validation_handler unsafe JSON encoding**: wrapped `exc.errors()` in `jsonable_encoder` so non-primitive ctx values (Pattern, Enum, exception instances) don't fall through to the 500 handler.
- **reachability hits non-existent /v1/health**: probe now tries `/v1/health`, `/health`, and `/` in order; first 2xx wins. Self-hosted Firecrawl versions that only respond at `/` no longer fail startup.
- **extraction.\_raise_for_status magic numbers**: replaced bounds arithmetic with `response.is_server_error` / `response.is_client_error`.
- **jobs.get_job_by_episode_id duplicated SELECTs**: collapsed to one query with an optional WHERE fragment.
- **pipeline two except blocks duplicated**: shared via `_finalize_failure` helper.
- **extraction trailing unreachable raise**: clarified the comment to acknowledge it's a type-checker satisfier.
- **fast_backoff fixture was dead code**: replaced with a real fixture that sets `FIRECRAWL_BACKOFF_BASE_SECONDS=0` and clears the settings cache so retry tests actually run fast; deduped from each consumer.
- **.env.example default `FIRECRAWL_URL=http://firecrawl:3002` resolved nowhere on a fresh clone**: changed default to `http://host.docker.internal:3002` matching the `OPENAI_BASE_URL` pattern.

New tests added by the review pass (11 more, 59 total):

- `test_reachability.py`: 6 tests covering check_firecrawl 2xx, fallback through endpoint candidates, network unreachable, persistent 5xx reports last detail, run_all raises on failure, run_all returns on success.
- `test_api_v1.py`: 5 new tests — `extra="forbid"` rejects typo'd field, raw URL preserved through to status, reprocess+inflight still returns 409 with the right reason, status endpoint returns failed jobs with stage + error, 500 envelope contract (no detail leakage, never logs exc.args into response).

- New tests (17 new, 59 total Phase 2 starting point pre-review):
  - `test_extraction.py`: happy path, retries-then-succeeds on 5xx, no-retry on 4xx, retry exhaustion on persistent 5xx, MIN_EXTRACTION_CHARS guard, `success=false` rejection. Uses `httpx.MockTransport` via a factory monkeypatch on `httpx.AsyncClient`.
  - `test_pipeline.py`: status=done with stage=extract on success; status=failed with stage+error on extraction error; status=failed with `JOB_TIMEOUT_SECONDS` error on timeout (last persisted stage reported).
  - `test_api_v1.py`: submit 201 + 12-char episode_id; submit 400 on invalid URL via the validation handler; submit 409 on in-flight duplicate; submit reprocess=true wipes prior episode and returns `replaced_previous=true`; status 200 with full envelope; status 404 with envelope.
  - `test_worker.py`: added `test_pickup_runs_pipeline_against_a_queued_job` (end-to-end with stubbed extractor) and `test_pickup_returns_false_when_no_queued_jobs`; existing `_crash_recovery` + run-loop tests still pass.
- Container smoke verified end-to-end with a mock Firecrawl on `:13002`: reachability check passes, `POST /api/v1/submit` returns 201, status progresses queued → done within seconds, structured logs include `pipeline_start`, `stage_start`, `extract_complete` (markdown_chars=1900, has_title=true), `stage_end` (duration_ms=7), `pipeline_done`. Contextvars correctly stamp `job_id` + `episode_id` + `stage` on every record.

### Added (Phase 1 - Project Scaffold)

- Repo layout per the build plan: `backend/app/{api,core,utils,prompts,corrections,reference}`, `backend/tests/`, `data/`.
- `pyproject.toml` (uv-managed, Python 3.13). Runtime deps: `fastapi`, `uvicorn[standard]`, `pydantic`, `pydantic-settings`. Dev deps live under PEP 735 `[dependency-groups].dev` so `uv sync --no-dev` excludes them in the image. Marked as a uv virtual project (no wheel build).
- `backend/app/version.py` as the single source of `__version__`.
- `backend/app/config.py`: Pydantic `BaseSettings` covering the required, conditional, and tunable env vars from the build plan. `extra="forbid"` so typo'd keys in `.env` fail loudly. Provider-specific validation: `openai-compatible` requires `OPENAI_BASE_URL` + `OPENAI_API_KEY`; `anthropic` requires `ANTHROPIC_API_KEY`. `get_settings()` is `lru_cache`-singletonized.
- `backend/app/utils/logging.py`: stdlib `logging` with custom `JSONFormatter` (Loki-ready, ms + Z timestamps) and `TextFormatter` (local dev). Context propagation via `contextvars` (`job_id_ctx`, `episode_id_ctx`, `stage_ctx`, `status_ctx`). Constant `service` label per build plan. Inverse-denylist context payload so every caller-supplied extra surfaces. Third-party loggers locked at WARNING.
- `backend/app/startup.py`: shared `bootstrap(settings, *, process_label)` called by both the FastAPI lifespan and the queue worker — single source of truth for logging + migrations + the startup banner.
- `backend/app/core/database.py`: sync `sqlite3` connection helper with WAL pragma + DELETE fallback, `synchronous=NORMAL`, `wal_autocheckpoint=1000`, `foreign_keys=ON`. Idempotent migration runner with `fcntl.flock` on `.migration.lock`, `schema_migrations` tracking table, explicit `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` so the migration body and tracking-row INSERT are atomic under autocommit, retry loop that refreshes the pending set between attempts on transient `database is locked`, and a `wal_checkpoint(TRUNCATE)`-before-copy backup that only triggers when a pending migration runs against a populated DB. `_db_has_user_tables` skips sqlite-internal bookkeeping tables. Includes crash-recovery `reset_processing_to_queued` that bumps `updated_at` and preserves any pre-existing error message via `COALESCE`. Includes backup pruning.
- v1 schema migration `001_initial_schema`: `jobs` + `episodes` tables with application-managed `updated_at`, the indices the build plan specifies, and the timestamp default convention (`strftime('%Y-%m-%dT%H:%M:%SZ', 'now')`).
- `backend/app/api/health.py`: `/health/live` (no dependency checks), `/health/ready` (DB connectivity check, returns 503 on failure and logs the exception with traceback), `/health` (alias registered as a stacked decorator on the same function). Both ready responses include `version`, `uptime_seconds` (per-app instance, read from `app.state.started_at` set in lifespan), `components.app`, `components.python`, and the `checks` map.
- `backend/app/main.py`: FastAPI app whose async lifespan calls `bootstrap(...)` and stamps `app.state.started_at`. Shutdown log line on lifespan exit.
- `backend/app/worker.py`: queue process skeleton. Registers `SIGTERM`/`SIGINT` via `loop.add_signal_handler` **before** crash recovery + migrations so a signal during a slow startup is caught, then loops on `shutdown.wait()`. Phase 2 wires real pickup logic onto this skeleton.
- `entrypoint.sh`: supervises `uvicorn` and `python -m app.worker`. Bounded cleanup wait (10s) with SIGKILL fallback so a child that ignores SIGTERM can't hold the container open. Always exits non-zero when one process exits (even a clean `exit 0`) so Docker `restart: unless-stopped` brings the supervised pair back.
- Multi-stage `Dockerfile`: Node stage stubbed (real Vite build in Phase 11), `python:3.13-slim` runtime, `uv` for dep install with `--no-dev --frozen` (build fails loudly on lockfile drift), non-root `audicle` user, healthcheck on `/health/live`.
- `docker-compose.yml` with the `app` service: ports, `env_file`, `host.docker.internal` extra-host for host-installed Ollama on Linux, bind mounts for `data/`, `prompts/`, `corrections/`, `reference/` (the reference mount lands now so Phase 4 picks it up automatically), healthcheck, `restart: unless-stopped`, `no-new-privileges`, `cap_drop: ALL`, log rotation.
- `.env.example` covering every Phase 1-applicable env var from the build plan, grouped by category.
- `.dockerignore` and `.gitignore` extended for Python, venv, runtime data, and editor artifacts. Project `CLAUDE.md` stays local-only per the original gitignore.
- Placeholder bind-mount targets: `backend/app/prompts/script.txt` (real prompt lands in Phase 3), `backend/app/corrections/pronunciation.json` (empty dict), `backend/app/reference/.gitkeep`.
- `backend/tests/`: 31 tests covering config validation (3 negative cases against pydantic `ValidationError`), structured logging (denylist passthrough, ms+Z timestamps, context-filter injection for all four ContextVars, idempotent setup with handler-count assertion), migration runner (idempotent, backup-on-pending-only, mid-migration rollback atomicity, retry loop recovery from transient lock, lock-serializes-concurrent-callers via threads, WAL+foreign_keys pragmas, prune backups), `reset_processing_to_queued` bumping `updated_at` and preserving prior error, `bootstrap()` safe-to-call-twice, worker `_crash_recovery` integration, TestClient-driven health endpoints (200 happy path + 503 on simulated DB failure).
- Container smoke verified: `docker compose build` succeeds, container boots, all three health endpoints return 200, structured logs include `service`, `process_label`, ms+Z timestamps, version/python/pid/hostname, no spurious backups across restart.

### Code-review pass (multi-agent /simplify + /code-review)

Findings surfaced by the multi-angle review and applied in this pass:

- **Migration atomicity**: `with conn:` is a no-op under `isolation_level=None`. Replaced with explicit `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` so the migration body and the `schema_migrations` INSERT are atomic. ROLLBACK wrapped in `contextlib.suppress(OperationalError)` so a secondary error from BEGIN/COMMIT failure can't mask the original.
- **Retry semantics**: the pending list is refreshed from `schema_migrations` between attempts so a transient lock can't trigger a duplicate INSERT (`UNIQUE` violation) on retry.
- **Backup safety**: `_backup_db` calls `PRAGMA wal_checkpoint(TRUNCATE)` before copy so the main `.db` file is self-contained and the `-wal`/`-shm` sidecars don't need to ride along.
- **Backup gating**: `_db_has_user_tables` filters sqlite-internal tables (`sqlite_*`) so a future `AUTOINCREMENT` migration doesn't trigger a spurious backup on a near-empty DB.
- **Crash recovery preserves errors**: `reset_processing_to_queued` uses `COALESCE(error, 'reset on restart')` so a real upstream failure recorded just before the worker crashed isn't overwritten.
- **Entrypoint contract**: supervisor exits non-zero on any child exit (even `exit 0`) so `restart: unless-stopped` always brings the pair back. Bounded cleanup with SIGKILL fallback.
- **Worker signal handlers**: installed via `loop.add_signal_handler` **before** crash recovery; `_shutdown` is loop-scoped (not module-level).
- **Bootstrap dedup**: extracted `app.startup.bootstrap(...)` so the lifespan and the worker share one setup-logging + run-migrations path.
- **Health aliases**: stacked `@router.get` decorators on a single function rather than two duplicate handlers.
- **Structured logging fixes**:
  - JSON timestamp now uses `formatTime(record)` without an explicit `datefmt` so `default_msec_format` actually applies — output is `YYYY-MM-DDTHH:MM:SS.NNNZ` instead of naive seconds.
  - `_context_payload` switched from a 5-key whitelist to a `_STANDARD_RECORD_ATTRS` denylist so every caller-supplied extra (`error`, `path`, `count`, `version`, `process_label`, ...) surfaces in JSON instead of being silently dropped.
  - Constant `service` label on every record (build plan low-cardinality label).
  - `stage_ctx` and `status_ctx` ContextVars added for spec parity (build plan calls these out as label/body fields propagated by context, not threaded through every `extra=`).
  - `_hostname()` switched from `lru_cache(maxsize=1)` to `functools.cache` (idiomatic on 3.9+).
- **Health endpoint visibility**: `_STARTED_AT` moved to `app.state.started_at` (per-app, set in lifespan) so multi-worker uvicorn reports consistent uptime. DB exception in readiness logs at WARNING with traceback so intermittent failures are visible in Loki.
- **Test isolation**: `env` fixture switched from `return` to `yield` with `get_settings.cache_clear()` on teardown, so a stale `Settings` pointing at a removed `tmp_path` can't bleed into the next test.
- **Settings strictness**: `extra="forbid"` so `.env` typos like `LLM_MODE=...` fail at startup instead of silently being ignored.
- **Docker layer**: bind mounts target `/app/app/*` (matching where the Dockerfile actually copies files); added `./backend/app/reference:/app/app/reference` so the volume exists before Phase 4 lands.
- **Dockerfile**: dropped silent `--frozen` fallback (`uv sync --no-dev --frozen` only — lockfile drift fails the build); migrated to PEP 735 `[dependency-groups]` so `--no-dev` actually excludes dev deps.
- **Lint**: enabled ruff rule sets `E,F,I,B,UP,SIM,RUF`. Repo is clean (`uv run ruff check .` returns "All checks passed").
- **New tests** added for the previously uncovered behaviors: mid-migration rollback atomicity, retry loop on transient lock, `updated_at` bump + prior-error preservation, lock serialization across threads, JSON timestamp ms+Z, JSON arbitrary extras pass-through, idempotent `setup_logging` handler count, `bootstrap()` safe-to-call-twice, worker `_crash_recovery` integration.
