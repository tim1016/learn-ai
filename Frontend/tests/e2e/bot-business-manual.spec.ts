import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

test('the manual supports chapter links, mobile reading and complete printing', async ({ page }) => {
  // A document needs no connected broker. Keep the shell's reads local to the test.
  await page.route('**/api/**', route => route.fulfill({ status: 503, json: { detail: 'Offline for document test' } }));
  await page.goto('/docs/bot-business-manual');
  const print = page.getByRole('button', { name: 'Print / Save PDF' });
  await expect(print).toBeEnabled();
  const chapters = page.getByRole('navigation', { name: 'Manual chapters' }).getByRole('link');
  for (const link of await chapters.all()) {
    const href = await link.getAttribute('href');
    expect(href).toBeTruthy();
    const id = href?.split('#')[1];
    expect(id).toBeTruthy();
    await expect(page.locator('article').locator('[id="' + id + '"]')).toHaveCount(1);
  }
  await chapters.filter({ hasText: 'Exits, Stop & Flatten' }).click();
  await expect(page).toHaveURL(/#exits-and-stopping$/);
  await expect(page.locator('#exits-and-stopping')).toBeInViewport();
  await expect(page).toHaveTitle('Bot Business Logic Manual');
  await expect(page.locator('.katex')).toHaveCount(0);

  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations.map(({ id, nodes }) => ({
    id, failures: nodes.map(node => node.failureSummary),
  }))).toEqual([]);

  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.locator('app-bot-manual').evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await chapters.filter({ hasText: 'Stocks, sizing & budgets' }).click();
  // The jump must land the heading just below the sticky bar. Sampling before
  // the router's anchor scroll fires reads the heading still below the fold,
  // which passed vacuously while the scroll really landed under the bar
  // (#3020) — so pin a window straddling the bar's bottom edge.
  await expect.poll(async () => {
    const heading = await page.locator('#stocks-and-budgets').boundingBox();
    const header = await page.locator('app-top-bar').boundingBox();
    if (!heading || !header) return false;
    const barBottom = header.y + header.height;
    return heading.y >= barBottom - 1 && heading.y <= barBottom + 120;
  }, { timeout: 15_000 }).toBe(true);

  await page.emulateMedia({ media: 'print' });
  await expect(print).toBeHidden();
  await expect(page.locator('app-top-bar')).toBeHidden();
  await expect(page.locator('article h2')).toHaveCount(await chapters.count());
  await expect(page.locator('#big-picture')).toBeVisible();
  await expect(page.locator('#source-notes')).toBeVisible();
  await page.emulateMedia({ media: 'screen' });
  await expect(print).toBeVisible();
});
