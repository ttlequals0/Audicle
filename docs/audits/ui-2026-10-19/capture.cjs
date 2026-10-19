const { chromium } = require('playwright');
const path = require('node:path');
const fs = require('node:fs/promises');
const assert = require('node:assert/strict');

const phase = process.argv[2];
if (!['before', 'after'].includes(phase)) throw new Error('Usage: node capture.cjs before|after');
const baseURL = process.env.AUDICLE_URL || 'http://127.0.0.1:5173';

(async () => {
  const browser = await chromium.launch({ channel: 'chrome' });
  const report = { phase, baseURL, pages: [] };
  try {
    for (const [device, viewport] of Object.entries({ desktop: { width: 1440, height: 1000 }, mobile: { width: 390, height: 844 } })) {
      const context = await browser.newContext({ viewport, deviceScaleFactor: 1, reducedMotion: 'reduce' });
      const page = await context.newPage();
      for (const route of ['settings', 'home', 'feed']) {
        await page.goto(`${baseURL}/${route === 'home' ? '' : route}`);
        await page.getByRole('heading', { level: 1 }).waitFor();
        if (route === 'settings') await page.getByRole('button', { name: /^Extraction/ }).waitFor();
        if (route === 'feed') await page.getByText(/no episodes yet\./i, { exact: true }).waitFor();
        await page.evaluate(() => document.fonts.ready);
        if (phase === 'after') {
          assert.ok(await page.evaluate(() => [...document.fonts].some(font => font.family === 'Satoshi' && font.status === 'loaded')), 'Satoshi must load before capturing the updated UI');
        }
        await page.screenshot({ path: path.join(__dirname, 'screenshots', `${phase}-${route}-${device}.png`) });
        report.pages.push(await page.evaluate(({ route, device }) => ({
          route, device, width: innerWidth, height: innerHeight,
          pageHeight: document.documentElement.scrollHeight,
          overflow: document.documentElement.scrollWidth > innerWidth,
          font: getComputedStyle(document.body).fontFamily,
          loadedFonts: [...document.fonts].filter(font => font.status === 'loaded').map(font => font.family),
          headings: [...document.querySelectorAll('h1,h2,h3')].filter(el => el.checkVisibility()).map(el => el.querySelector('.section-title')?.textContent ?? el.textContent),
        }), { route, device }));
        if (route === 'settings') {
          await page.getByRole('searchbox', { name: 'Search settings' }).fill('CHAPTERS_TARGET_SECONDS');
          await page.getByLabel('Target chapter length (seconds)', { exact: true }).waitFor();
          await page.screenshot({ path: path.join(__dirname, 'screenshots', `${phase}-settings-search-${device}.png`) });
        }
      }
      await context.close();
    }
    await fs.writeFile(path.join(__dirname, `${phase}.json`), JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify(report, null, 2));
  } finally {
    await browser.close();
  }
})();
