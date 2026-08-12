# LLM providers

The LLM does four jobs: cleaning the extracted page down to the article, the pronunciation pass in normalize, the episode summary, and picking chapter titles. All four go through one configured provider.

Set the provider, model, and key in Settings under LLM (or env; see [Environment variables](environment-variables.md#llm-provider)). Everything here is live-tunable: switch providers and the next job uses the new one. Keys are stored masked, and the model dropdown fetches the provider's live model list.

| Provider | Needs | Notes |
|---|---|---|
| `openai-compatible` | `OPENAI_BASE_URL`, `OPENAI_API_KEY` | Any server speaking the OpenAI chat API: llama.cpp, vLLM, LM Studio, a hosted service |
| `anthropic` | `ANTHROPIC_API_KEY` | Base URL is fixed |
| `openrouter` | `OPENROUTER_API_KEY` | Base URL is fixed |
| `ollama` | `OLLAMA_BASE_URL` | Typically `http://host.docker.internal:11434/v1` for Ollama on the compose host |

Provider-specific fields only show in the UI under their selected provider, so the form never asks for a key the current provider cannot use.

## Tuning

`LLM_TEMPERATURE`, `LLM_MAX_TOKENS`, `LLM_TIMEOUT_SECONDS`, and `LLM_RETRY_COUNT` apply to every call. `LLM_CLEANUP_WINDOW_CHARS` bounds how much of a long article each cleanup call sees, and `LLM_PRONUNCIATION_CONCURRENCY` parallelizes the normalize pass.

A local model is entirely workable: cleanup and summary are not demanding tasks. If cleaned articles come back truncated or mangled, look at the model's context length against your typical article size before blaming the prompt.

The cleanup prompt itself is editable in Settings (cleanup prompt section), and the chapter prompt via `/api/v1/prompt?kind=chapters`.

[< Docs index](README.md)
