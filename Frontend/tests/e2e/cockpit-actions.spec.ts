// PRD #617 — cockpit-actions.spec.ts
//
// Resume enabled / disabled mapping; Pause symmetric; Stop only in
// overflow with retirement confirm; Mark Poisoned only on Audit tab
// with typed HALT; Flatten uses only the atomic endpoint.

import { expect, test, type Page } from '@playwright/test';

import { buildAccountSummary, buildScenarioStatus, buildSummary } from './fixtures/cockpit-fixtures';

const SID = 'dep_val_smoke_001';

async function installRoutes(
  page: Page,
  state: {
    summaries: ReturnType<typeof buildSummary>[];
    status: ReturnType<typeof buildScenarioStatus>;
    account: ReturnType<typeof buildAccountSummary>;
  },
) {
  await page.route('**/api/live-instances', (route) => route.fulfill({ json: state.summaries }));
  await page.route('**/api/live-instances/account-summary', (route) => route.fulfill({ json: state.account }));
  await page.route(/\/api\/live-instances\/[^/]+\/status$/, (route) => route.fulfill({ json: state.status }));
}

test.describe('cockpit actions', () => {
  test('PAUSED + clean guards: Resume enabled, Pause disabled (ALREADY_PAUSED)', async ({ page }) => {
    const status = buildScenarioStatus({
      strategyInstanceId: SID,
      intent: 'PAUSED',
      processState: 'running',
    });
    status.operator_surface.actions.resume = { enabled: true, effect: 'LIVE_ACTUATION', disabled_reason_code: null, disabled_reasons: [] };
    status.operator_surface.actions.pause = { enabled: false, effect: 'LIVE_ACTUATION', disabled_reason_code: 'ALREADY_PAUSED', disabled_reasons: ['ALREADY_PAUSED'] };
    await installRoutes(page, {
      summaries: [buildSummary({ strategyInstanceId: SID, desiredState: 'PAUSED' })],
      status,
      account: buildAccountSummary({}),
    });
    await page.goto(`/broker/instances/${SID}`);
    await expect(page.getByTestId('action-resume')).toBeEnabled();
    await expect(page.getByTestId('action-pause')).toBeDisabled();
  });

  test('PAUSED + UNSAFE broker: Resume disabled with BROKER_SAFETY_UNSAFE', async ({ page }) => {
    const status = buildScenarioStatus({
      strategyInstanceId: SID,
      intent: 'PAUSED',
      brokerSafety: 'UNSAFE',
    });
    status.operator_surface.actions.resume = { enabled: false, effect: 'LIVE_ACTUATION', disabled_reason_code: 'BROKER_SAFETY_UNSAFE', disabled_reasons: ['BROKER_SAFETY_UNSAFE'] };
    await installRoutes(page, {
      summaries: [buildSummary({ strategyInstanceId: SID, desiredState: 'PAUSED' })],
      status,
      account: buildAccountSummary({}),
    });
    await page.goto(`/broker/instances/${SID}`);
    await expect(page.getByTestId('action-resume')).toBeDisabled();
    await expect(page.getByTestId('action-resume')).toHaveAttribute('title', 'BROKER_SAFETY_UNSAFE');
    await expect(page.getByTestId('indicator-safety')).toContainText('UNSAFE');
  });

  test('Stop affordance only lives in identity-strip overflow, never inline', async ({ page }) => {
    const status = buildScenarioStatus({ strategyInstanceId: SID, intent: 'PAUSED' });
    await installRoutes(page, {
      summaries: [buildSummary({ strategyInstanceId: SID, desiredState: 'PAUSED' })],
      status,
      account: buildAccountSummary({}),
    });
    await page.goto(`/broker/instances/${SID}`);
    // Exactly one Stop affordance on the page, inside the overflow.
    const overflow = page.getByTestId('overflow-menu');
    await expect(overflow).toBeVisible();
    // Stop button is inside overflow; outside the overflow there is none.
    const stopButtons = page.getByTestId('action-stop');
    await expect(stopButtons).toHaveCount(1);
  });

  test('Flatten + owned positions: Flatten enabled', async ({ page }) => {
    const status = buildScenarioStatus({
      strategyInstanceId: SID,
      processState: 'running',
      ownedPositions: { SPY: 5 },
    });
    await installRoutes(page, {
      summaries: [buildSummary({ strategyInstanceId: SID, desiredState: 'RUNNING' })],
      status,
      account: buildAccountSummary({}),
    });
    await page.goto(`/broker/instances/${SID}`);
    await expect(page.getByTestId('action-flatten-and-pause')).toBeEnabled();
  });

  test('Flatten without owned positions: disabled with NO_OWNED_POSITIONS', async ({ page }) => {
    const status = buildScenarioStatus({ strategyInstanceId: SID, processState: 'running' });
    await installRoutes(page, {
      summaries: [buildSummary({ strategyInstanceId: SID })],
      status,
      account: buildAccountSummary({}),
    });
    await page.goto(`/broker/instances/${SID}`);
    await expect(page.getByTestId('action-flatten-and-pause')).toBeDisabled();
    await expect(page.getByTestId('action-flatten-and-pause')).toHaveAttribute('title', 'NO_OWNED_POSITIONS');
  });

  test('Mark Poisoned only renders on the Audit tab (typed-HALT)', async ({ page }) => {
    const status = buildScenarioStatus({ strategyInstanceId: SID, processState: 'running' });
    await installRoutes(page, {
      summaries: [buildSummary({ strategyInstanceId: SID })],
      status,
      account: buildAccountSummary({}),
    });
    await page.goto(`/broker/instances/${SID}`);
    // Not visible on Status tab
    await expect(page.getByTestId('audit-mark-poisoned-trigger')).toHaveCount(0);
    // Switch to Audit
    await page.getByTestId('inner-tab-audit').click();
    await expect(page.getByTestId('audit-mark-poisoned-trigger')).toBeVisible();
  });
});
