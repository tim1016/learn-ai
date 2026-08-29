import { expect, test } from '@playwright/test';

/**
 * Layout coverage for the top-bar menubar that replaced the sidebar in
 * #1853. The Vitest suite runs in jsdom, which does no layout, so the
 * collapsed panel's width is only observable from a real viewport —
 * this is the surface that regressed after #1853 shipped green.
 */

/** Every group AppMenu exposes, in menubar order (see app/shell/app-menu.ts). */
const GROUPS = [
  'Data Lab',
  'Options',
  'Research',
  'Edge Analysis',
  'Alpaca',
  'Strategy Tools',
  'Documentation',
] as const;

/** Straddles AppMenubarComponent's 1150px collapse breakpoint. */
const WIDE = { width: 1440, height: 900 };
const NARROW = { width: 900, height: 900 };

test.describe('Shell menubar', () => {
  test('lays every group out inline above the collapse breakpoint', async ({ page }) => {
    await page.setViewportSize(WIDE);
    await page.goto('/data-lab');

    const menubar = page.getByRole('navigation', { name: 'Primary' });
    for (const group of GROUPS) {
      await expect(menubar.getByRole('menuitem', { name: group })).toBeVisible();
    }

    await expect(page.getByRole('button', { name: 'Navigation' })).toBeHidden();
  });

  test('opens a panel wide enough to read below the collapse breakpoint', async ({ page }) => {
    await page.setViewportSize(NARROW);
    await page.goto('/data-lab');

    const toggle = page.getByRole('button', { name: 'Navigation' });
    await expect(toggle).toBeVisible();
    await toggle.click();

    const panel = page.getByRole('menubar');
    await expect(panel).toBeVisible();

    // The regression: PrimeNG sizes this panel `width: 100%` of the
    // menubar, which here wraps only the toggle. The panel opened at
    // 24px and squeezed every entry to 14px — icon only, no readable
    // label. Nothing overflowed, so `toBeVisible()` still passed; the
    // measured widths are what actually catch it.
    const panelBox = await panel.boundingBox();
    expect(panelBox).not.toBeNull();
    expect(panelBox!.width).toBeGreaterThanOrEqual(200);

    for (const group of GROUPS) {
      const entry = panel.getByRole('menuitem', { name: group });
      await expect(entry).toBeVisible();

      const entryBox = await entry.boundingBox();
      expect(entryBox).not.toBeNull();
      expect(entryBox!.width).toBeGreaterThanOrEqual(120);
    }
  });

  test('expands a group inside the collapsed panel', async ({ page }) => {
    await page.setViewportSize(NARROW);
    await page.goto('/data-lab');

    await page.getByRole('button', { name: 'Navigation' }).click();

    const panel = page.getByRole('menubar');
    await panel.getByRole('menuitem', { name: 'Edge Analysis' }).click();

    await expect(panel.getByRole('menuitem', { name: 'Regimes' })).toBeVisible();
  });
});
