# Audicle: Logo Spec (Locked)

## Brand

**Name:** Audicle (audio + article)

**Tone:** Technical, restrained. Developer tool, not consumer brand.

## Mark

Custom letter "A" with a five-bar audio waveform inside the counter. The waveform reads as an audio level meter: short, medium, tall, medium, short. Symmetric, balanced, legible at small sizes.

The A is constructed from two straight strokes meeting at an apex. No traditional crossbar; the waveform replaces it.

## Palette

| Role | Hex | Notes |
|------|-----|-------|
| Primary green | `#1ce783` | Used for the mark on dark backgrounds |
| Rich black | `#040405` | Primary background, mark on light/green backgrounds |
| Off-white | `#f5f5f5` | Optional light background |

Inspired by Hulu's brand palette. Single accent color, no gradients.

## Primary Lockup

Green mark on rich-black background. This is the canonical version used for podcast artwork and the README header.

## Variants

| Variant | Foreground | Background | Use |
|---------|------------|------------|-----|
| Primary | `#1ce783` | `#040405` | Default. Podcast artwork. Dark UI. |
| Inverted | `#040405` | `#1ce783` | Marketing accents, callouts. |
| Mono light | `#040405` | `#f5f5f5` | Print, light UI, documentation. |
| Mono dark | `#f5f5f5` | `#040405` | Dark UI without the green accent. |

## Typography

**Wordmark:** "audicle" in lowercase, Inter Semibold, letter-spacing -1.2.

**System font fallback:** `Inter, system-ui, -apple-system, sans-serif`.

## Asset Files

Saved at `backend/app/assets/`:

- `mark.svg` -- icon only, primary green on transparent
- `mark-mono.svg` -- icon only, currentColor (inherits from CSS)
- `wordmark.svg` -- mark + "audicle" text, primary green
- `wordmark-mono.svg` -- same, currentColor
- `podcast-artwork.svg` -- 3000x3000 square for feed-level artwork (rasterize to PNG before serving)
- `favicon.svg` -- 16x16 optimized version with thicker strokes

## Rasterization for Podcast Artwork

Apple Podcasts requires PNG or JPG. Rasterize `podcast-artwork.svg` to:

- `podcast-artwork-3000.png` (feed-level, 3000x3000)
- `podcast-artwork-1400.png` (fallback, 1400x1400)

Use `rsvg-convert`, ImageMagick, or Inkscape to export.

## Construction Reference

### Icon SVG (160x160 viewBox)

```svg
<svg viewBox="0 0 160 160" xmlns="http://www.w3.org/2000/svg">
  <path d="M 30 130 L 80 26 L 130 130"
        stroke="#1ce783" stroke-width="10" fill="none"
        stroke-linejoin="round" stroke-linecap="round"/>
  <line x1="56" y1="100" x2="56" y2="112" stroke="#1ce783" stroke-width="6" stroke-linecap="round"/>
  <line x1="68" y1="86"  x2="68" y2="112" stroke="#1ce783" stroke-width="6" stroke-linecap="round"/>
  <line x1="80" y1="74"  x2="80" y2="112" stroke="#1ce783" stroke-width="6" stroke-linecap="round"/>
  <line x1="92" y1="86"  x2="92" y2="112" stroke="#1ce783" stroke-width="6" stroke-linecap="round"/>
  <line x1="104" y1="100" x2="104" y2="112" stroke="#1ce783" stroke-width="6" stroke-linecap="round"/>
</svg>
```

### Stroke Weights

- A outline: `10` (icon), scales linearly with viewBox
- Waveform bars: `6` (icon), scales linearly with viewBox
- At 16px favicon size: A outline `14`, bars `10` (thickened to remain legible)

### Bar Heights

Center bar tallest. Symmetric pattern: 12 / 26 / 38 / 26 / 12 units. Bar spacing 12 units. Center bar at x=80 (the visual center of the A).

## Usage Rules

- Safe zone around the mark: equal to the width of one waveform bar on all sides
- Do not skew, rotate, or distort
- Do not change the bar heights or symmetry
- Do not place the mark on busy photographic backgrounds without solid backing
- When mark and wordmark appear together, mark is left of wordmark, vertically centered to the wordmark cap height
