# Audicle UI design guide

Use this guide for new UI work and changes to existing screens. Keep Home focused on submitting articles and Feed focused on listening. Settings uses categories and progressive disclosure so configuration does not become a single long form.

## Brand reference

[Hulu's brand guide](https://www.thisishulu.com/) specifies green `#1ce783`, rich black `#040405`, and the Graphik type family. Audicle uses those colors with its own mark and Satoshi as the approved font fallback. Satoshi is not Graphik. Do not add Graphik files without a suitable webfont license.

Use flat, dark surfaces for the application. Green identifies primary actions, selected navigation and focus. Keep promotional gradients and decorative textures out of forms.

## Source files

| File | Responsibility |
| --- | --- |
| [tokens.json](tokens.json) | Canonical colors, font families and corner radii |
| [tokens.css](tokens.css) | Generated CSS variables, imported by the frontend |
| [sync_design_tokens.mjs](../scripts/sync_design_tokens.mjs) | Generates CSS and checks for drift during the production build |
| [index.css](../frontend/src/index.css) | Shared controls, typography, spacing and responsive layouts |
| [tailwind.config.cjs](../frontend/tailwind.config.cjs) | Tailwind names mapped to the same CSS variables |
| [Settings.tsx](../frontend/src/routes/Settings.tsx) | Category membership, section summaries and search metadata |
| [CollapsibleSection.tsx](../frontend/src/components/CollapsibleSection.tsx) | Accessible disclosure behavior and editor draft retention |

Change a token in `tokens.json`, then run `node scripts/sync_design_tokens.mjs`. The build rejects stale generated CSS. Use existing classes and semantic color utilities before adding new CSS. Do not copy hex values into components.

## Color

| Token | Value | Use |
| --- | --- | --- |
| `primary` | `#1ce783` | Primary actions, active navigation, focus, success |
| `primary_pressed` | `#16d076` | Hovered or pressed primary controls |
| `background` | `#040405` | Page canvas |
| `paper` | `#0a0a0c` | Cards and sections |
| `surface` | `#15151a` | Inputs and inset surfaces |
| `line` | `#26262e` | Dividers and quiet borders |
| `line_strong` | `#3a3a45` | Hovered cards and menu boundaries |
| `text` | `#f5f5f5` | Headings, labels and body text |
| `text_dim`, `text_mute` | `#9a9aaa` | Secondary text and help |
| `control` | `#6b6b78` | Inactive switch thumb; never helper text |
| `danger` | `#ff5252` | Errors and destructive actions |
| `danger_surface` | `#4a1f1f` | Error tags and destructive borders |
| `warning_text` / `warning_surface` | `#fcd67a` / `#3b2b0a` | Processing status and warning notices |

Use black text on filled green controls. Pair status colors with a readable label; color alone must not communicate success, failure or selection. Use opacity only for disabled controls, not secondary text.

Normal text must meet the [WCAG minimum contrast ratio of 4.5:1](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html). These pairs are calculated from the token values using relative luminance, rounded here to two decimal places:

| Pair | Contrast ratio |
| --- | --- |
| Primary text on page background | 18.80:1 |
| Secondary text on card background | 7.14:1 |
| Secondary text on input background | 6.57:1 |
| Black text on green action | 12.50:1 |
| Processing text on processing background | 9.79:1 |

## Typography

Use `font-sans` for headings, navigation, labels, explanations, buttons and episode metadata. It resolves to Satoshi, then system fonts. Reserve `font-mono` for code editors and raw diagnostic values. Home, Feed, login, settings help, player controls and notices use Satoshi throughout. The wordmark uses Satoshi at weight 700 with `-0.02em` tracking.

Load Satoshi through the single-family Fontshare URL in `frontend/index.html`. The previous combined Satoshi/JetBrains Mono request returned an empty stylesheet. Do not assume a computed font-family proves that a webfont loaded: verify a loaded Satoshi face in `document.fonts`. The capture script checks this before saving after images. Fontshare access is required; if it is unavailable, the system sans fallback remains readable. Font files are not committed to this repository.

The following scale uses the default root size of 16 px. Use rem units in CSS so browser text settings still apply.

| Role | Size | Weight | Example |
| --- | --- | --- | --- |
| Home hero | 2.25 rem / 36 px | 900 | Drop a link |
| Page title | 1.5 rem / 24 px | 900 | Settings |
| Category heading | 1.25 rem / 20 px | 700 | Publishing |
| Section heading | 1 rem / 16 px | 700 | Chapters |
| Form value | 0.9375 rem / 15 px | 400 | Desktop text input |
| Body, helper, label, navigation | 0.875 rem / 14 px | 400, 500 or 700 | Section summary, field label |
| Compact action or navigation detail | 0.8125 rem / 13 px | 400 or 700 | Category description |
| Compact metadata | 0.75 rem / 12 px | 400 or 500 | Version, duration, episode source |

Touch-device form controls use at least 16 px to avoid automatic input zoom. Use sentence case for new labels. Keep acronyms such as API, RSS and TTS uppercase. Show units in labels, such as "Target chapter length (seconds)". Do not use environment variable names as the only user-facing label.

## Layout and spacing

| Rule | Value |
| --- | --- |
| Home and Feed maximum width | 42 rem / 672 px |
| Settings maximum width | 64 rem / 1024 px |
| Desktop settings navigation | 13 rem / 208 px |
| Settings column gap | 2 rem / 32 px |
| Mobile settings breakpoint | Below 768 px |
| Page side padding | 1 rem / 16 px |
| Page top padding | 1.5 rem / 24 px |
| Section horizontal padding | 1.25 rem / 20 px desktop; 1 rem / 16 px mobile |
| Related row or card gap | 0.75 rem / 12 px |
| Major group gap | 1.5 rem / 24 px |
| Small / medium / large radius | 6 px / 8 px / 12 px |
| Interactive target | At least 44 px high; icon buttons also at least 44 px wide |

Keep a single content column inside settings categories. On smaller screens, replace the side navigation with the labeled native category selector. Allow descriptions and save feedback to wrap. Do not squeeze the desktop navigation into horizontally scrolling tabs.

## Components and states

| Component | Rules |
| --- | --- |
| `.btn-primary` | Filled green with black text. Use for the main action in a form. Show progress and disable while submitting. |
| `.btn-ghost` | Secondary action with a quiet border. Buttons, links and upload labels share its inline-flex target. Upload labels show focus when their hidden file input is focused. |
| `.btn-danger` | Reserve for deletion or destructive changes. Explain consequences and retain existing confirmation behavior. |
| Search | Use the shared magnifying-glass icon and clear action. Clear and Escape reset the query and return focus to the input. |
| `.field` and `.label` | Every control needs an associated label. Placeholders supplement labels. Keep focus visible and secret fields masked. |
| `.toggle` | Use a button with `role="switch"`, `aria-checked` and a readable name. Retain the visible keyboard focus outline. |
| `.section` | Show a short heading and a one-sentence summary before expansion. Avoid repeating long instructions in the collapsed header. |
| `.card` | Use for feed or job content. Do not make a passive card look like a button. |
| `.tag` | Keep the status word visible. Processing and queued animation stops with reduced motion enabled. |
| `.save-bar` | Appears for pending generic settings or save feedback. Remains available across categories; reports the total unsaved count. |
| Native audio | Keep browser controls for voice and chime previews. Use `AudioPlayer` for episodes. Browser-owned controls may use platform typography. |
| Reset to defaults | Secondary action for generic settings. It changes the draft and still requires Save settings. Prompt resets and clearing corrections save immediately. |

Loading must say what is loading. Errors must explain the failure and offer the available recovery action. Empty search results must show the query and a way back. Use `role="alert"` for errors and `role="status"` for nonblocking results.

## Settings information architecture

| Category | Put these settings here |
| --- | --- |
| Content | Article extraction, website overrides, cleanup, prompts, uploaded documents |
| Voice | Reference voices, speech generation, pronunciation, verification, audio output |
| Publishing | Podcast metadata, artwork, chapters, RSS access, episode retention |
| Services | AI provider, endpoint URLs, external request budgets, webhooks, sidecar configuration |
| System | Admin access, login/session policies, job limits, logging, version and diagnostics |

- Put every section in exactly one category. Add a summary that answers what the user can change there.
- Default to Content. Use `/settings?category=publishing` for a category link. Unknown categories fall back to Content.
- Search all categories regardless of the selection. Match section names, summaries, readable labels and technical keys. Add widget control names to `WIDGET_SEARCH_TERMS`. Keep provider-dependent fields scoped to the selected provider.
- Open matching sections during search, then restore the previous disclosure state when search clears. Escape and Clear search return focus to the search input.
- Preserve drafts across category changes, collapsed sections, search and browser Back while Settings remains mounted. Reloading or leaving Settings still discards unsaved drafts.
- Mount an editor when first opened; retain it while hidden. Do not load every widget merely to search its text.
- Include every runtime key in a group or dedicated widget, with a readable label and search coverage.
- Keep generic settings saves and independent widget saves distinct. Changing categories must not save anything automatically.

Follow the [WAI disclosure pattern](https://www.w3.org/WAI/ARIA/apg/patterns/disclosure/): use a button, expose `aria-expanded`, and connect it to its body with `aria-controls`. Use native navigation buttons and a native mobile selector. Do not claim tab semantics without implementing the corresponding keyboard behavior.

## Review before merging

- Check the affected screens at desktop and narrow mobile widths, plus browser zoom. Confirm no horizontal overflow, overlapping actions or clipped focus.
- Test keyboard navigation, visible focus, labels, search clearing and reduced motion. Verify any new color pairing.
- Test saved, unsaved, loading, error, empty and disabled states affected by the change. Use the real backend for screenshots.
- For settings, test edits across category changes, search, collapse and browser history. Confirm hidden widgets retain their drafts and are absent from keyboard navigation.
- Run `npm test` and `npm run build` from `frontend/`. Reuse the [browser audit scripts](../docs/audits/ui-2026-10-19/README.md) for navigation changes.
- Include comparable before/after screenshots in UI PRs. Record the viewport, data state and limits of the verification.

The [UI audit](../docs/audits/ui-2026-10-19/README.md) records the findings behind these rules and the original comparisons.
