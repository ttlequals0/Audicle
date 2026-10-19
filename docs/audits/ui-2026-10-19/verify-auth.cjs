const assert = require('node:assert/strict');
const { randomUUID } = require('node:crypto');
const path = require('node:path');
const { chromium, webkit } = require('playwright');

if (process.env.AUDICLE_AUTH_AUDIT !== '1') throw new Error('Set AUDICLE_AUTH_AUDIT=1 only with a disposable local audit database.');
const baseURL = process.env.AUDICLE_URL || 'http://127.0.0.1:5173';

(async () => {
  for (const [engine, browserType] of Object.entries({ chromium, webkit })) {
    const browser = await browserType.launch(engine === 'chromium' ? { channel: 'chrome' } : {});
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, reducedMotion: 'reduce' });
    const password = randomUUID();
    const request = async (route, method, data) => page.evaluate(async ({ route, method, data }) => {
      const token = document.cookie.split('; ').find(value => value.startsWith('audicle_csrf='))?.split('=')[1];
      const response = await fetch(`/api/v1/${route}`, {
        method, headers: { 'Content-Type': 'application/json', ...(token ? { 'X-CSRF-Token': token } : {}) },
        body: data ? JSON.stringify(data) : undefined,
      });
      return { status: response.status, body: await response.json() };
    }, { route, method, data });
    let passwordSet = false;
    let secureCookieOverride;
    let cookiePolicyChanged = false;
    try {
      await page.goto(baseURL);
      await page.getByRole('heading', { level: 1 }).waitFor();
      const status = await request('auth/status', 'GET');
      assert.equal(status.body.password_set, false, 'Use an audit database without an existing password');
      const settings = await request('settings', 'GET');
      secureCookieOverride = settings.body.values.SESSION_COOKIE_SECURE ?? null;
      if (new URL(baseURL).protocol === 'http:') {
        assert.ok(['127.0.0.1', 'localhost'].includes(new URL(baseURL).hostname));
        assert.equal((await request('settings', 'PUT', { SESSION_COOKIE_SECURE: false })).status, 200);
        cookiePolicyChanged = true;
      }
      assert.equal((await request('auth/password', 'PUT', { new_password: password })).status, 200);
      passwordSet = true;
      assert.equal((await request('auth/logout', 'POST')).status, 200);
      await page.reload();
      await page.getByRole('heading', { name: 'Sign in' }).waitFor();
      await page.evaluate(() => document.fonts.ready);
      assert.ok(await page.getByRole('heading').evaluate(el => getComputedStyle(el).fontFamily.startsWith('Satoshi')));
      assert.ok(await page.evaluate(() => [...document.fonts].some(font => font.family === 'Satoshi' && font.status === 'loaded')));
      await page.getByLabel('Password', { exact: true }).fill(randomUUID());
      await page.getByRole('button', { name: 'Sign in', exact: true }).click();
      await page.getByRole('alert').filter({ hasText: 'Invalid password' }).waitFor();
      assert.ok(await page.getByRole('alert').evaluate(el => getComputedStyle(el).fontFamily.startsWith('Satoshi')));
      if (engine === 'chromium') await page.screenshot({ path: path.join(__dirname, 'screenshots/after-login-mobile.png') });
      await page.getByLabel('Password', { exact: true }).fill(password);
      await page.getByRole('button', { name: 'Sign in', exact: true }).click();
      await page.getByRole('navigation', { name: 'Primary' }).waitFor();
      await page.goto(`${baseURL}/settings?category=system`);
      await page.getByRole('button', { name: 'Admin access', exact: true }).click();
      await page.getByLabel('Current password', { exact: true }).fill(randomUUID());
      await page.getByLabel('New password (minimum 8 characters)', { exact: true }).fill(randomUUID());
      await page.getByRole('button', { name: 'Change password', exact: true }).click();
      const error = page.getByRole('alert').filter({ hasText: 'Current password is incorrect' });
      await error.waitFor();
      assert.ok(await error.evaluate(el => el.classList.contains('text-danger')));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      if (engine === 'chromium') {
        await page.getByLabel('Current password', { exact: true }).fill('');
        await page.getByLabel('New password (minimum 8 characters)', { exact: true }).fill('');
        await page.screenshot({ path: path.join(__dirname, 'screenshots/after-admin-mobile.png'), fullPage: true });
      }
      console.log(`${engine}: real sign-in, failed password feedback, Satoshi and mobile layout passed`);
    } finally {
      if (passwordSet) {
        const cleared = await request('auth/password', 'PUT', { current_password: password, new_password: '' });
        assert.equal(cleared.status, 200, 'Restore the disposable database to open mode');
      }
      if (cookiePolicyChanged) {
        assert.equal((await request('settings', 'PUT', { SESSION_COOKIE_SECURE: secureCookieOverride })).status, 200);
      }
      await browser.close();
    }
  }
})();
