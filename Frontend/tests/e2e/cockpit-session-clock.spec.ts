// PRD #617 — cockpit-session-clock.spec.ts
//
// Clock pill shows phase and a CLOCK DIFFERENCE advisory when offset
// exceeds 30s.  Boundary-aligned refresh after next_transition_ms.

import { expect, test, type Page } from '@playwright/test';

import { buildAccountSummary, buildScenarioStatus, buildSummary } from './fixtures/cockpit-fixtures';

const SID = 'dep_val_smoke_001';

async function installRoutes(page: Page, status: ReturnType<typeof buildScenarioStatus>) {
  await page.route('**/api/live-instances', (route) =>
    route.fulfill({ json: [buildSummary({ strategyInstanceId: SID })] }),
  );
  await page.route('**/api/live-instances/account-summary', (route) =>
    route.fulfill({ json: buildAccountSummary({}) }),
  );
  await page.route(/\/api\/live-instances\/[^/]+\/status$/, (route) => route.fulfill({ json: status }));
}

test.describe('cockpit session + clock', () => {
  test('Clock pill shows session phase', async ({ page }) => {
    const status = buildScenarioStatus({ strategyInstanceId: SID });
    await installRoutes(page, status);
    await page.goto(`/broker/instances/${SID}`);
    await expect(page.getByTestId('clock-pill')).toBeVisible();
    await expect(page.getByTestId('session-phase')).toContainText('RTH');
  });

  test('CLOCK DIFFERENCE advisory fires when server clock differs by > 30s', async ({ page }) => {
    const status = buildScenarioStatus({ strategyInstanceId: SID });
    // Mark fetched_at_ms far in the future so the offset > 30_000 ms.
    status.fetched_at_ms = Date.now() + 600_000;
    status.operator_surface.trading_session.as_of_ms = status.fetched_at_ms;
    await installRoutes(page, status);
    await page.goto(`/broker/instances/${SID}`);
    await expect(page.getByTestId('clock-difference-advisory')).toBeVisible();
  });

  test('Indicators stay independent across PROCESS / INTENT / READINESS / BROKER / SAFETY', async ({ page }) => {
    // Meta-rule (ADR-0013 §3) explicit assertion.
    const status = buildScenarioStatus({
      strategyInstanceId: SID,
      processState: 'idle',
      intent: 'RUNNING',
      readinessVerdict: 'BLOCKED',
      brokerSafety: 'UNSAFE',
      brokerConnection: 'DISCONNECTED',
    });
    await installRoutes(page, status);
    await page.goto(`/broker/instances/${SID}`);
    await expect(page.getByTestId('indicator-process')).toContainText('WAITING_FOR_HOST');
    await expect(page.getByTestId('indicator-intent')).toContainText('RUNNING');
    await expect(page.getByTestId('indicator-readiness')).toContainText('BLOCKED');
    await expect(page.getByTestId('indicator-broker')).toContainText('DISCONNECTED');
    await expect(page.getByTestId('indicator-safety')).toContainText('UNSAFE');
    // No synthetic master status anywhere.
    await expect(page.locator('[data-testid="bot-status-banner"]')).toHaveCount(0);
  });
});
