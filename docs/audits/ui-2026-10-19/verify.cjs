const assert = require('node:assert/strict');
const { chromium, webkit } = require('playwright');

const baseURL = process.env.AUDICLE_URL || 'http://127.0.0.1:5173';

(async () => {
  for (const [engine, browserType] of Object.entries({ chromium, webkit })) {
    if (process.env.AUDICLE_ENGINE && process.env.AUDICLE_ENGINE !== engine) continue;
    const browser = await browserType.launch(engine === 'chromium' ? { channel: 'chrome' } : {});
    try {
      for (const width of [1440, 768, 390, 320]) {
        if (process.env.AUDICLE_WIDTH && Number(process.env.AUDICLE_WIDTH) !== width) continue;
        const context = await browser.newContext({ viewport: { width, height: 900 }, reducedMotion: 'reduce' });
        const page = await context.newPage();
        const errors = [];
        const writes = [];
        page.on('pageerror', error => errors.push(error.message));
        page.on('request', request => {
          if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) writes.push(request.url());
        });
        await page.goto(`${baseURL}/settings`);
        await page.getByRole('button', { name: 'Extraction', exact: true }).waitFor();
        assert.equal(await page.getByRole('heading', { name: 'Content', exact: true }).count(), 1);
        assert.equal(await page.getByRole('heading', { name: 'Voice', exact: true }).count(), 0);
        const choose = async name => {
          const select = page.getByLabel('Settings category', { exact: true });
          if (await select.isVisible()) await select.selectOption(name);
          else await page.getByRole('navigation', { name: 'Settings categories' }).getByRole('button', { name: new RegExp(`^${name}`) }).click();
          await page.getByRole('heading', { name, exact: true, level: 2 }).waitFor();
        };
        await page.getByRole('button', { name: 'Cleanup prompt', exact: true }).click();
        const prompt = page.getByRole('textbox', { name: 'Cleanup prompt', exact: true });
        const originalPrompt = await prompt.inputValue();
        const draftPrompt = `${originalPrompt}\n`;
        await prompt.fill(draftPrompt);
        await page.getByRole('button', { name: 'Cleanup prompt', exact: true }).click();
        await page.getByRole('button', { name: 'Cleanup prompt', exact: true }).click();
        assert.equal(await prompt.inputValue(), draftPrompt, 'Collapse must preserve the editor draft');
        await choose('Publishing');
        assert.match(page.url(), /category=publishing/);
        await page.getByRole('button', { name: 'Chapters', exact: true }).click();
        const target = page.getByLabel('Target chapter length (seconds)', { exact: true });
        const original = await target.inputValue();
        await target.fill(String(Number(original) + 1));
        await page.getByText('1 unsaved change', { exact: true }).waitFor();
        await choose('System');
        await page.goBack();
        await target.waitFor();
        assert.equal(await target.inputValue(), String(Number(original) + 1), 'Back navigation must preserve the settings draft');
        const search = page.getByRole('searchbox', { name: 'Search settings' });
        for (const query of ['target chapter length', 'CHAPTERS_TARGET_SECONDS', 'chapter target length']) {
          await search.fill(query);
          await target.waitFor();
          assert.equal(await target.inputValue(), String(Number(original) + 1));
        }
        await search.fill('zzzz-no-matching-setting');
        await page.getByText('No settings match "zzzz-no-matching-setting".', { exact: true }).waitFor();
        assert.equal(await page.getByRole('button', { name: 'Chapters', exact: true }).count(), 0);
        await search.press('Escape');
        assert.equal(await search.inputValue(), '');
        assert.equal(await search.evaluate(el => document.activeElement === el), true);
        await target.fill(original);
        assert.equal(await page.getByRole('button', { name: 'Save settings', exact: true }).count(), 0);
        await choose('Content');
        assert.equal(await prompt.inputValue(), draftPrompt, 'Category and search changes must preserve the editor draft');
        await prompt.fill(originalPrompt);
        await search.fill('password');
        await page.getByRole('button', { name: 'Admin access', exact: true }).waitFor();
        await page.getByRole('button', { name: 'Clear search', exact: true }).click();
        assert.equal(await search.evaluate(el => document.activeElement === el), true);
        for (const [query, section] of [
          ['RENDER_NAV_TIMEOUT_MS', 'Sidecar settings'],
          ['page navigation timeout', 'Sidecar settings'],
          ['cookie jar', 'Site overrides'],
          ['audition sample text', 'Voices'],
          ['CHIME_ENABLED', 'End chime'],
          ['FEED_AUTH_KEY', 'Authenticated feeds'],
        ]) {
          await search.fill(query);
          await page.getByRole('button', { name: section, exact: true }).waitFor();
        }
        await search.press('Escape');
        if (await page.getByRole('navigation', { name: 'Settings categories' }).isVisible()) {
          const voice = page.getByRole('navigation', { name: 'Settings categories' }).getByRole('button', { name: /^Voice/ });
          await voice.focus();
          await page.keyboard.press('Enter');
          await page.waitForURL('**/settings?category=voice');
          await page.locator('.settings-nav button[aria-pressed="true"]').filter({ hasText: 'Voice' }).waitFor();
          assert.equal(await voice.getAttribute('aria-pressed'), 'true');
        }
        for (const category of ['Content', 'Voice', 'Publishing', 'Services', 'System']) {
          await choose(category);
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${category} overflows at ${width}px`);
          const buttons = page.locator('#settings-sections section:not([hidden]) > .section:not([hidden]) > h3 > button');
          for (const button of await buttons.all()) {
            const bodyId = await button.getAttribute('aria-controls');
            assert.ok(bodyId);
            assert.equal(await page.locator(`[id="${bodyId}"]`).count(), 1);
            if (await button.getAttribute('aria-expanded') === 'false') await button.click();
          }
          if (category === 'Content') {
            await page.getByLabel('Cleanup window (characters)', { exact: true }).waitFor();
          }
          if (category === 'Voice') {
            for (const [backend, key] of [['TTS_BACKEND', 'TTS_API_KEY'], ['WHISPER_BACKEND', 'WHISPER_API_KEY']]) {
              const select = page.locator(`#${backend}`);
              const original = await select.inputValue();
              await select.selectOption('openai-api');
              assert.ok(await page.locator(`#${key}`).evaluate(el => el.classList.contains('field-secret')));
              await select.selectOption(original);
            }
          }
          if (category === 'Services') await page.getByRole('button', { name: 'Save sidecar settings', exact: true }).waitFor();
          const outside = await page.locator('#settings-sections input:not(.sr-only), #settings-sections textarea, #settings-sections select, #settings-sections button, #settings-sections .btn-ghost').evaluateAll(elements => elements.filter(el => {
            if (!el.getClientRects().length) return false;
            const rect = el.getBoundingClientRect();
            return rect.left < -1 || rect.right > innerWidth + 1;
          }).map(el => el.id || el.textContent || el.getAttribute('aria-label')));
          assert.deepEqual(outside, [], `${category} expanded controls must fit at ${width}px`);
          const wrongFonts = await page.locator('#settings-sections .label, #settings-sections button, #settings-sections p').evaluateAll(elements => elements.filter(el => el.getClientRects().length && !getComputedStyle(el).fontFamily.startsWith('Satoshi')).map(el => el.textContent));
          assert.deepEqual(wrongFonts, [], 'Labels, help and controls must use Satoshi');
        }
        for (const route of ['', 'feed']) {
          await page.goto(`${baseURL}/${route}`);
          await page.getByRole('heading', { level: 1 }).waitFor();
          await page.evaluate(() => document.fonts.ready);
          assert.ok(await page.evaluate(() => [...document.fonts].some(font => font.family.includes('Satoshi') && font.status === 'loaded')), 'Satoshi must load on Home and Feed');
          assert.ok(await page.getByRole('heading', { level: 1 }).evaluate(el => getComputedStyle(el).fontFamily.startsWith('Satoshi')));
          assert.equal(await page.locator('#main-content [class*="mono"]').count(), 0, 'Home and Feed must use the UI font');
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
          if (route === 'feed') {
            const search = page.getByRole('searchbox', { name: 'Search episodes' });
            await search.fill('no-matching-episode');
            await search.press('Escape');
            assert.equal(await search.inputValue(), '');
            await search.fill('no-matching-episode');
            await page.getByRole('button', { name: 'Clear search', exact: true }).click();
            assert.equal(await search.evaluate(el => document.activeElement === el), true);
          }
        }
        assert.deepEqual(errors, [], 'No uncaught browser errors');
        assert.deepEqual(writes, [], 'Browsing and drafts must not write to the backend');
        console.log(`${engine} ${width}px: categories, history, search, drafts, keyboard, expanded controls, disclosures, Satoshi passed`);
        await context.close();
      }
    } catch (error) {
      const page = browser.contexts().at(-1)?.pages().at(-1);
      if (page) console.error(await page.evaluate(() => ({ url: location.href, width: innerWidth, mobile: matchMedia('(max-width: 767px)').matches, error: document.querySelector('[role="alert"]')?.textContent })));
      throw error;
    } finally {
      await browser.close();
    }
  }
})();
