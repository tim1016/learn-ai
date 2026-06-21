// PRD #607 cockpit revision 2026-06-21 — sticky-header E2E.
//
// Asserts that <header class="cockpit-sticky"> pins to top:0 when the
// page scrolls.  This was the canary for the regression where
// position: sticky on the nested sticky-control-bar never engaged
// because the parent ``.main { overflow-x: auto }`` scroll-ancestor
// chain was broken.

import { expect, test } from '@playwright/test';

import steadyFixture from '../../src/testing/operator_surface_fixtures/steady.json' assert { type: 'json' };

test.describe('cockpit sticky header', () => {
  test('cockpit-sticky stays pinned at top: 0 after vertical scroll', async ({
    page,
  }) => {
    await page.route('**/api/live-instances/*/status', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(steadyFixture),
      });
    });

    await page.goto('/broker/instances?id=spy_ema_paper');

    const sticky = page.getByTestId('cockpit-sticky');
    await expect(sticky).toBeVisible();

    const before = await sticky.boundingBox();
    expect(before).not.toBeNull();
    const topBefore = before!.y;

    // Scroll the page far enough that a non-sticky header would
    // disappear above the fold.
    await page.evaluate(() => window.scrollTo(0, 1200));

    const after = await sticky.boundingBox();
    expect(after).not.toBeNull();
    const topAfter = after!.y;

    // The sticky header's top edge must be unchanged (within 1px for
    // sub-pixel rendering) — it stays pinned to top: 0 instead of
    // scrolling away.
    expect(Math.abs(topAfter - topBefore)).toBeLessThanOrEqual(1);
  });
});
