import { expect, test } from '@playwright/test';

for (const manual of [
  { route: '/docs/options-backtest-manual', section: 'capital', topic: 'collateral', other: '/docs/broker-manual' },
  { route: '/docs/broker-manual', section: 'fees', topic: 'OCC', other: '/docs/options-backtest-manual' },
]) {
  test(`${manual.route} supports offline reading, chapter search, mobile jumps and complete printing`, async ({ page }) => {
    await page.route('**/api/**', route => route.fulfill({ status: 503, json: { detail: 'Offline for manual test' } }));
    await page.goto(manual.route);
    const book = page.locator('app-trader-manual');
    const print = book.getByRole('button', { name: 'Print / Save PDF' });
    await expect(print).toBeEnabled();
    const chapterLinks = book.getByRole('navigation', { name: 'Manual chapters' }).getByRole('link');
    const allLinks = await chapterLinks.all();
    for (const link of allLinks) {
      const href = await link.getAttribute('href');
      const fragment = href?.split('#')[1];
      expect(fragment).toBeTruthy();
      await expect(book.locator(`article h2[id="${fragment}"]`)).toHaveCount(1);
    }
    await book.getByRole('searchbox').fill(manual.topic);
    await expect(chapterLinks).toHaveCount(1);
    await chapterLinks.click();
    await expect(page).toHaveURL(`${manual.route}#${manual.section}`);
    await expect(book.locator(`#${manual.section}`)).toBeInViewport();
    await expect(book.getByRole('searchbox')).toBeInViewport();
    await expect(book.locator('.katex')).toHaveCount(0);

    await page.setViewportSize({ width: 390, height: 844 });
    await expect(book.getByRole('searchbox')).toBeHidden();
    await book.getByRole('button', { name: 'Browse chapters' }).click();
    await book.getByRole('button', { name: 'Show all chapters' }).click();
    await expect(chapterLinks).toHaveCount(allLinks.length);
    await chapterLinks.last().click();
    await expect(page).toHaveURL(`${manual.route}#glossary`);
    await expect(book.getByRole('searchbox')).toBeHidden();
    await expect(book.locator('#glossary')).toBeInViewport();
    expect(await book.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);

    await page.emulateMedia({ media: 'print' });
    await expect(print).toBeHidden();
    await expect(page.locator('app-top-bar')).toBeHidden();
    await expect(book.locator('article h2')).toHaveCount(allLinks.length);
    await expect(book.locator('article h2').first()).toBeVisible();
    await expect(book.locator('article h2').last()).toBeVisible();
    await page.emulateMedia({ media: 'screen' });
    await page.setViewportSize({ width: 1280, height: 900 });
    await book.locator('.companion').click();
    await expect(page).toHaveURL(manual.other);
    await expect(book.getByRole('button', { name: 'Print / Save PDF' })).toBeEnabled();
  });
}

test('a failed manual download cannot print an empty document', async ({ page }) => {
  await page.route('**/api/**', route => route.fulfill({ status: 503, json: {} }));
  await page.route('**/assets/docs/options-backtest-manual.md', route => route.fulfill({ status: 503, body: 'Unavailable' }));
  await page.goto('/docs/options-backtest-manual');
  await expect(page.getByRole('button', { name: 'Print / Save PDF' })).toBeDisabled();
  await expect(page.locator('app-trader-manual .md-error')).toBeVisible();
});
