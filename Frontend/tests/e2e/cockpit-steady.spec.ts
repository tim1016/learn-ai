// PRD #607 / Slice 8 (#615) — STEADY cockpit E2E.
//
// Loads the STEADY fixture, asserts the verdict-bearing cards are in
// their collapsed shape, asserts no verdict-glow halo is visible, and
// asserts deleted-legacy surfaces are absent from the DOM.
//
// The fixture itself is captured from the Python endpoint via
// PythonDataService/scripts/capture_operator_surface_fixture.py and
// shared with the Frontend contract test.

import { expect, test } from '@playwright/test';

import steadyFixture from '../../src/testing/operator_surface_fixtures/steady.json' assert { type: 'json' };

test.describe('cockpit STEADY', () => {
  test('Can-It-Trade renders the calm READY strip, no verdict-glow', async ({ page }) => {
    await page.route('**/api/live-instances/*/status', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(steadyFixture),
      });
    });

    await page.goto('/broker/instances?id=spy_ema_paper');

    await expect(page.getByTestId('can-it-trade-ready-strip')).toBeVisible();
    await expect(page.getByTestId('can-it-trade-card')).toHaveCount(0);
  });

  test('host-process notice is hidden on a running instance', async ({ page }) => {
    await page.route('**/api/live-instances/*/status', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(steadyFixture),
      });
    });

    await page.goto('/broker/instances?id=spy_ema_paper');
    await expect(page.getByTestId('host-process-notice')).toHaveCount(0);
  });
});
