# Audicle Planning Package

Planning and design artifacts for **Audicle** (audio + article), a self-hosted service that turns article URLs into a Podcasting 2.0 feed.

Tagline: *Your reading list, as a podcast you own.*

Repo: https://github.com/ttlequals0/Audicle

## Contents

| File | What it is |
|------|------------|
| `build-plan.md` | The full build plan. Architecture, data model, pipeline, API conventions, auth, UI, RSS, audio, deployment, and a 13-phase build order. Start here. |
| `ui-mockup.html` | Mobile-first, three-tab UI mockup (Home / Feed / Settings). Open in a browser; tabs are clickable. Reflects the locked design system (Satoshi + JetBrains Mono, dark green-on-black). |
| `logo-spec.md` | Logo construction reference: the "A" mark with five-bar waveform crossbar. |
| `branding/` | Canonical brand assets and design tokens. Single source of truth for backend and frontend. |

## branding/

| File | Purpose |
|------|---------|
| `README.md` | Palette, typography, usage, trademark note. |
| `tokens.json` | Design tokens (color, typography, radius) in machine-readable form. |
| `tokens.css` | Same tokens as CSS custom properties for the frontend. |
| `mark.svg`, `mark-mono.svg` | Icon only. |
| `wordmark.svg`, `wordmark-mono.svg` | Mark + "audicle" text. |
| `podcast-artwork.svg`, `podcast-artwork-3000.png`, `podcast-artwork-1400.png` | Podcast cover art (Apple sizes). |
| `favicon.svg`, `favicon-32.png`, `favicon-16.png` | Favicons. |

## Status

This is a planning package, not code. The build plan is the spec to implement against. Every major decision in it was reviewed section by section and locked.

## License

Code (once written) is intended to be MIT. The Audicle name and logo are reserved; see `branding/README.md`. XTTS-v2 model weights are CPML (non-commercial); see the build plan's License Notes.
