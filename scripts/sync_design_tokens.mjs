import { readFileSync, writeFileSync } from 'node:fs';

const tokens = JSON.parse(readFileSync(new URL('../branding/tokens.json', import.meta.url), 'utf8'));
const destination = new URL('../branding/tokens.css', import.meta.url);
const declarations = [];
for (const [name, token] of Object.entries(tokens.color)) {
  declarations.push(`  --audicle-color-${name.replaceAll('_', '-')}: ${token.value};`);
}
for (const family of ['sans', 'mono']) {
  declarations.push(`  --audicle-font-${family}: ${tokens.typography[`family_${family}`].value};`);
}
for (const [name, token] of Object.entries(tokens.typography.letter_spacing)) {
  declarations.push(`  --audicle-letter-spacing-${name}: ${token.value};`);
}
for (const [name, suffix] of Object.entries({ small: 'sm', medium: 'md', large: 'lg' })) {
  declarations.push(`  --audicle-radius-${suffix}: ${tokens.radius[name].value};`);
}
const css = `/* Generated from branding/tokens.json by scripts/sync_design_tokens.mjs. */\n:root {\n${declarations.join('\n')}\n}\n`;
if (process.argv.includes('--check')) {
  if (readFileSync(destination, 'utf8') !== css) {
    throw new Error('Design tokens are stale. Run: node scripts/sync_design_tokens.mjs');
  }
} else {
  writeFileSync(destination, css);
}
