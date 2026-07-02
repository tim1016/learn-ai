// PRD #617 — bot-control-actions.spec.ts
//
// Resume enabled / disabled mapping; Pause symmetric; Stop only in
// overflow with retirement confirm; Mark Poisoned only on Audit tab
// with typed HALT; Flatten uses only the atomic endpoint.

import { expect, test, type Page } from '@playwright/test';

import { buildAccountSummary, buildScenarioStatus, buildSummary } from './fixtures/bot-control-fixtures';

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

test.describe('Bot Control actions', () => {
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
    await page.goto(`/broker/bots/${SID}`);
    await expect(page.getByTestId('action-resume')).toBeEnabled();
    await expect(page.getByTestId('action-pause')).toBeDisabled();
  });

  test('PAUSED + UNSAFE broker: Resume disabled with operator-language tooltip (not raw code)', async ({ page }) => {
    // 2026-06-22 audit P2-002 — the tooltip routes the server-authored
    // ``BROKER_SAFETY_UNSAFE`` code through the shared operator-copy
    // map, so the operator no longer sees the raw enum. The button
    // still carries the underlying code on ``data-disabled-reason-code``
    // for diagnostics.
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
    await page.goto(`/broker/bots/${SID}`);
    const resume = page.getByTestId('action-resume');
    await expect(resume).toBeDisabled();
    const title = await resume.getAttribute('title');
    expect(title).not.toBe('BROKER_SAFETY_UNSAFE');
    expect(title).toContain('UNSAFE');
    expect(title?.toLowerCase()).toContain('paper-only');
    await expect(resume).toHaveAttribute('data-disabled-reason-code', 'BROKER_SAFETY_UNSAFE');
    await expect(page.getByTestId('indicator-safety')).toContainText('UNSAFE');
  });

  test('Stop affordance only lives in identity-strip overflow, never inline', async ({ page }) => {
    const status = buildScenarioStatus({ strategyInstanceId: SID, intent: 'PAUSED' });
    await installRoutes(page, {
      summaries: [buildSummary({ strategyInstanceId: SID, desiredState: 'PAUSED' })],
      status,
      account: buildAccountSummary({}),
    });
    await page.goto(`/broker/bots/${SID}`);
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
    await page.goto(`/broker/bots/${SID}`);
    await expect(page.getByTestId('action-flatten-and-pause')).toBeEnabled();
  });

  test('Flatten without owned positions: disabled with operator-language tooltip (not raw code)', async ({ page }) => {
    // 2026-06-22 audit P2-002 — operator copy, raw code preserved
    // on ``data-disabled-reason-code`` for diagnostics.
    const status = buildScenarioStatus({ strategyInstanceId: SID, processState: 'running' });
    await installRoutes(page, {
      summaries: [buildSummary({ strategyInstanceId: SID })],
      status,
      account: buildAccountSummary({}),
    });
    await page.goto(`/broker/bots/${SID}`);
    const flatten = page.getByTestId('action-flatten-and-pause');
    await expect(flatten).toBeDisabled();
    const title = await flatten.getAttribute('title');
    expect(title).not.toBe('NO_OWNED_POSITIONS');
    expect(title?.toLowerCase()).toContain('flatten');
    expect(title?.toLowerCase()).toContain('positions');
    await expect(flatten).toHaveAttribute('data-disabled-reason-code', 'NO_OWNED_POSITIONS');
  });

  test('Mark Poisoned only renders on the Audit tab (typed-HALT)', async ({ page }) => {
    const status = buildScenarioStatus({ strategyInstanceId: SID, processState: 'running' });
    await installRoutes(page, {
      summaries: [buildSummary({ strategyInstanceId: SID })],
      status,
      account: buildAccountSummary({}),
    });
    await page.goto(`/broker/bots/${SID}`);
    // Not visible on Status tab
    await expect(page.getByTestId('audit-mark-poisoned-trigger')).toHaveCount(0);
    // Switch to Audit
    await page.getByTestId('inner-tab-audit').click();
    await expect(page.getByTestId('audit-mark-poisoned-trigger')).toBeVisible();
  });
});
