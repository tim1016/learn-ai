// PRD #607 / Slice 2 (#609) — cockpit BLOCKED state E2E.
//
// Intercepts ``GET /api/live-instances/{id}/status`` and injects a
// BLOCKED fixture.  Asserts the verdict-glow halo via computed style +
// ``[data-verdict]`` (NOT a mixin-derived class name).

import { expect, test } from '@playwright/test';

import stoppedFixture from '../../src/testing/operator_surface_fixtures/stopped.json' assert { type: 'json' };

// Build a BLOCKED variant from the stopped fixture by swapping the
// readiness verdict.  Keeps the fixture file the single source of
// truth for the wire shape.
const blockedFixture = (() => {
  const clone = JSON.parse(JSON.stringify(stoppedFixture));
  clone.readiness = {
    kind: 'live_readiness',
    as_of_ms: 1,
    source: 'engine',
    verdict: 'BLOCKED',
    summary: 'Blocked: broker_connection — disconnected.',
    gates: [
      {
        name: 'broker_connection',
        status: 'fail',
        severity: 'hard',
        detail: 'disconnected',
      },
    ],
    live_readiness_available: null,
    orders_used: null,
    orders_cap: null,
  };
  return clone;
})();

test.describe('cockpit BLOCKED', () => {
  test('Can-It-Trade card carries data-verdict=blocked and verdict-glow halo', async ({
    page,
  }) => {
    await page.route('**/api/live-instances/*/status', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(blockedFixture),
      });
    });

    await page.goto('/broker/instances?id=spy_ema_paper');

    const card = page.locator('app-can-it-trade-card[data-verdict="blocked"]');
    await expect(card).toBeVisible();

    const styles = await card.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { borderWidth: cs.borderWidth, boxShadow: cs.boxShadow };
    });
    expect(styles.borderWidth).toBe('2px');
    expect(styles.boxShadow).not.toBe('none');
  });
});
