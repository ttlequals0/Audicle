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

Brand accent `#1ce783` (primary) on rich black `#040405`, a single accent with no
gradients, inspired by Hulu's brand identity. The full token set (surfaces, lines,
text, status colors) and contrast ratios are in `tokens.json` and the
[design guide](design-guide.md#color).

## Typography

Satoshi (weights 400, 500, 700, 900, loaded via Fontshare) is used throughout the
interface. It is the approved fallback for Hulu's Graphik, which is not bundled.
Inter is avoided to keep the brand distinctive. The system monospace stack is for
code editors and raw diagnostic values only. The wordmark is lowercase Satoshi 700
with letter-spacing -0.02em. Sizes and rules are in the
[design guide](design-guide.md#typography).

## Primary lockup

Green mark on rich-black background. Used for podcast artwork, README header, and any "official" Audicle context.

## Mark construction

Custom letter "A" with a five-bar audio waveform replacing the crossbar. Bars are symmetric, center-tallest: 12 / 26 / 38 / 26 / 12 units at 160-unit icon scale.

Stroke weights scale linearly with the viewBox: at the 160-unit icon scale the A outline is 10 and the waveform bars are 6, thickened to 14 / 10 for the 16 px favicon. The SVG sources in this directory are the canonical construction reference.

## Trademark

The Audicle name and logo are reserved. The MIT license covers code only, not the brand. Forks redistributed publicly should use a different name and logo.
