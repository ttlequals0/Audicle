# Audicle Branding

Canonical brand assets for Audicle. Single source of truth across backend, frontend, and any other consumer.

For application layouts, component states, accessibility and settings navigation, follow the [UI design guide](design-guide.md).

## Contents

| File | Purpose |
|------|---------|
| `mark.svg`, `mark-mono.svg` | Icon only. Mono uses `currentColor` for CSS-driven theming. |
| `wordmark.svg`, `wordmark-mono.svg` | Mark + "audicle" text. |
| `podcast-artwork.svg` | Source for podcast cover art. |
| `podcast-artwork-3000.png` | Rasterized cover, 3000x3000, Apple Podcasts max size. |
| `podcast-artwork-3000.jpg` | JPEG (RGB, 3000x3000) export of `podcast-artwork-3000.png`. Served as the default feed cover via its raw-GitHub URL so podcast apps cache a stable, extension-clean `.jpg` (`DEFAULT_ARTWORK_URL`). |
| `podcast-artwork-1400.png` | Rasterized cover, 1400x1400, Apple Podcasts min size. |
| `favicon.svg` | Optimized for small sizes (thicker strokes). |
| `favicon-32.png`, `favicon-16.png` | Rasterized favicons. |
| `tokens.json` | Design tokens (color, typography, radius). Machine-readable. |
| `tokens.css` | Generated CSS custom properties, imported by the frontend. Run `node scripts/sync_design_tokens.mjs` after editing `tokens.json`. |

## Palette

| Token | Hex | Role |
|-------|-----|------|
| primary | `#1ce783` | Brand accent. Mark on dark, UI highlights. |
| background | `#040405` | Page background. |
| paper | `#0a0a0c` | Card/section background. |
| surface | `#15151a` | Input fields. |
| line | `#26262e` | Borders, dividers. |
| text | `#f5f5f5` | Primary text. |
| text_dim | `#9a9aaa` | Secondary text. |
| text_mute | `#9a9aaa` | Helper text and muted labels. |
| danger | `#ff5252` | Errors, destructive actions. |

Palette inspired by Hulu's brand identity (green-on-black, single accent, no gradients).

## Typography

Use Satoshi throughout the interface, including Home and Feed metadata. Use system monospace only for code editors and raw diagnostic values.

**Sans (display, navigation, labels, UI body):** Satoshi. Loaded via Fontshare. Weights: 400, 500, 700, 900. This is the approved fallback for Hulu's Graphik; Graphik is not bundled.

**Mono (code and diagnostics):** the system monospace stack. Do not use it for page navigation, notices, episode metadata or player controls.

Inter is explicitly avoided to keep the brand distinctive.

Wordmark uses Satoshi 700 with letter-spacing -0.02em, lowercase.

## Primary Lockup

Green mark on rich-black background. Used for podcast artwork, README header, and any "official" Audicle context.

## Mark Construction

Custom letter "A" with a five-bar audio waveform replacing the crossbar. Bars are symmetric, center-tallest: 12 / 26 / 38 / 26 / 12 units at 160-unit icon scale.

Stroke weights scale linearly with the viewBox: at the 160-unit icon scale the A outline is 10 and the waveform bars are 6, thickened to 14 / 10 for the 16 px favicon. The SVG sources in this directory are the canonical construction reference.

## Trademark

The Audicle name and logo are reserved. The MIT license covers code only, not the brand. Forks redistributed publicly should use a different name and logo.
