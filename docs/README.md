# Audicle documentation

Full documentation for Audicle. Start with the [project README](../README.md) for the pitch, screenshots, and a quick install, then come here for the details.

## Contents

- [How it works](how-it-works.md) - the pipeline from URL to episode: extraction cascade, LLM cleanup, chunking, TTS, the quality gates, and the wrapper's memory ladder
- [Installation](installation.md) - requirements, quickstart, CPU-only deployment, first-run configuration
- [Web interface](web-interface.md) - the Home, Feed, and Settings pages, with screenshots
- [Configuration](configuration.md) - every settings category, what saves where, runtime versus env-only, per-card reset
- [Environment variables](environment-variables.md) - every variable, grouped, with the runtime-editable ones flagged
- [Voices and TTS](voices-and-tts.md) - reference voice slots, models and languages, the end-of-episode chime, and running Chatterbox or Whisper on another host
- [LLM providers](llm-providers.md) - the four providers, their keys, and what the LLM is used for
- [Feeds and Podcasting 2.0](feeds-and-podcasting.md) - the RSS feed, authenticated feeds, chapters, artwork, iTunes categories, retention
- [API and webhooks](api-and-webhooks.md) - the REST surface and the episode webhooks
- [Paywalled articles](paywalls.md) - bypass strategies, the render sidecar, registration walls, subscriber cookie jars
- [Glossary](glossary.md) - every term the app uses, defined and linked to the page that covers it
- [Releasing](releasing.md) - how a release is versioned, built, gated, and shipped
- [Deployment runbook](DEPLOYMENT.md) - health checks, logs, rollback, disk, and what the common failures mean

[< Project README](../README.md)
