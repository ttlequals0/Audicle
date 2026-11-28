# LLM providers

The LLM does four jobs: cleaning the extracted page down to the article, the pronunciation pass in normalize, the episode summary, and choosing chapter boundaries and titles. All four go through one configured provider.

Set the provider, model, and key under Settings > Services > LLM (or env; see [Environment variables](environment-variables.md#llm-provider)). Everything here is live-tunable: switch providers and the next job uses the new one. API keys are masked when read back. The model dropdown fetches the provider's live model list and sorts it alphabetically. Use "Test provider" before saving to check a draft URL and credential without generating content.

| Provider | Needs | Notes |
|---|---|---|
| `openai-compatible` | `OPENAI_BASE_URL`, `OPENAI_API_KEY` | Any server speaking the OpenAI chat API: llama.cpp, vLLM, LM Studio, a hosted service |
| `anthropic` | `ANTHROPIC_API_KEY` | Base URL is fixed |
| `openrouter` | `OPENROUTER_API_KEY` | Base URL is fixed |
| `ollama` | `OLLAMA_BASE_URL` | Typically `http://host.docker.internal:11434/v1` for Ollama on the compose host |

Provider-specific fields show in the UI only under their selected provider.

## Tuning

`LLM_TEMPERATURE` (default 0.7), `LLM_MAX_TOKENS` (16000), `LLM_TIMEOUT_SECONDS` (300), and `LLM_RETRY_COUNT` (3) apply to every call. `LLM_REASONING_EFFORT` (`none` by default) controls reasoning on models that support it. Leave it at `none`: a thinking model can spend its token budget before it writes the narration text.

`LLM_CLEANUP_WINDOW_CHARS` (12000) bounds how much of a long article each cleanup call sees, and `LLM_PRONUNCIATION_CONCURRENCY` (4) parallelizes the normalize pass.

A local model works: cleanup and summary are not demanding tasks. If cleaned articles come back truncated or mangled, look at the model's context length against your typical article size before blaming the prompt.

The cleanup prompt is editable under Settings > Content > Cleanup prompt. All four prompts (`cleanup`, `summary`, `pronunciation`, `chapters`) can be read, replaced, and reset through `/api/v1/prompt?kind=<name>`; `cleanup` is the default.

[< Docs index](README.md)
