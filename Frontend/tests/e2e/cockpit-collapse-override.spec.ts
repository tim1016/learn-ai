// PRD #607 / Slice 2 (#609) — cockpit collapse-override E2E.
//
// On READY cards the operator can manually expand; the expansion
// persists across consecutive READY status updates.  When the next
// status flips the verdict to ATTENTION the collapse-toggle disappears
// from the DOM (Option A — attention cards cannot be manually
// collapsed) and the operator override is cleared.
//
// Slice 2 ships the FOUNDATIONS for this behavior (verdict-glow mixin
// + collapsible-card pattern).  The Can-It-Trade card today renders a
// calm READY strip vs full attention card with NO toggle button (the
// design's Option A); this spec asserts that the toggle is absent on
// every attention verdict so a future refactor cannot silently
// introduce one.  Slices 4/5 add cards with a real READY-toggle path
// and extend this spec accordingly.

import { expect, test } from '@playwright/test';

import steadyFixture from '../../src/testing/operator_surface_fixtures/steady.json' assert { type: 'json' };
import stoppedFixture from '../../src/testing/operator_surface_fixtures/stopped.json' assert { type: 'json' };

test.describe('cockpit collapse override', () => {
  test('Can-It-Trade has no toggle on attention verdicts (Option A)', async ({
    page,
  }) => {
    const attentionFixture = (() => {
      const clone = JSON.parse(JSON.stringify(stoppedFixture));
      clone.readiness = {
        kind: 'live_readiness',
        as_of_ms: 1,
        source: 'engine',
        verdict: 'DEGRADED',
        summary: 'Degraded: data_provenance — soft warn.',
        gates: [
          {
            name: 'data_provenance',
            status: 'fail',
            severity: 'soft',
            detail: 'fallback feed',
          },
        ],
        live_readiness_available: null,
        orders_used: null,
        orders_cap: null,
      };
      return clone;
    })();

    await page.route('**/api/live-instances/*/status', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(attentionFixture),
      });
    });

    await page.goto('/broker/instances?id=spy_ema_paper');

    const card = page.locator('app-can-it-trade-card[data-verdict="degraded"]');
    await expect(card).toBeVisible();

    // No collapse / expand toggle in the DOM under attention verdicts.
    await expect(
      card.getByRole('button', { name: /collapse|expand/i }),
    ).toHaveCount(0);
  });

  test('READY verdict renders the calm strip, not a card body', async ({ page }) => {
    await page.route('**/api/live-instances/*/status', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(steadyFixture),
      });
    });

    await page.goto('/broker/instances?id=spy_ema_paper');

    // READY strip is rendered; full card body is absent.
    await expect(page.getByTestId('can-it-trade-ready-strip')).toBeVisible();
    await expect(page.getByTestId('can-it-trade-card')).toHaveCount(0);
  });
});
