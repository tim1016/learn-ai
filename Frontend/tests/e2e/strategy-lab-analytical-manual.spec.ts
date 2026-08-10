import { expect, test } from '@playwright/test';

test.describe('Strategy Lab analytical manual', () => {
  test('supports keyboard-addressable search, filters, contextual links, and a non-runtime case study', async ({ page }) => {
    await page.goto('/strategy-lab/docs?metric=sharpe&variant=sharpe.lean_native.v1&producer=lean_native&contract=lean-statistics-oracle-v1&run=900001');

    await expect(page.getByRole('heading', { name: 'Sharpe ratio' })).toBeVisible();
    await expect(page.getByText('Run 130: illustrative snapshot')).toBeVisible();
    await expect(page.getByText(/not a contract fixture/i)).toBeVisible();

    const search = page.getByRole('searchbox', { name: 'Search metric definitions' });
    await search.focus();
    await page.keyboard.type('profitFactor');
    await expect(page.getByRole('link', { name: /profit factor/i }).first()).toBeVisible();

    await page.getByRole('button', { name: 'Clear filters' }).click();
    await page.getByLabel('Producer').selectOption('lean_native');
    await expect(page.getByText(/documented variant/)).toBeVisible();
    await page.getByLabel('Used by this run').check();
    await expect(page.getByRole('link', { name: 'Sharpe ratio Lean Native', exact: true })).toBeVisible();

    await page.getByRole('button', { name: 'Clear filters' }).click();
    await expect(search).toHaveValue('');
    await expect(page.getByRole('link', { name: /Compare with/i })).toBeVisible();
  });

  test('keeps unknown metric and stale contract context explicit', async ({ page }) => {
    await page.goto('/strategy-lab/docs?metric=unknown_metric&contract=retired-v0');

    await expect(page.getByText(/requested metric is not documented/i)).toBeVisible();
  });
});
