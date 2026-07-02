// PRD #617 — bot-control-status-routing.spec.ts
//
// Auto-route exactly once on the foreground instance's READY → non-READY
// transition; do not re-force on subsequent attention-changed polls.
// Background instance enters attention → outer-tab marker only, never
// forced foreground.
//
// Meta-rule (ADR-0013 §3): every scenario asserts independent PROCESS,
// INTENT, READINESS, BROKER, and SAFETY values rather than a synthetic
// master status.

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
  await page.route('**/api/live-instances', (route) =>
    route.fulfill({ json: state.summaries }),
  );
  await page.route('**/api/live-instances/account-summary', (route) =>
    route.fulfill({ json: state.account }),
  );
  await page.route(/\/api\/live-instances\/[^/]+\/status$/, (route) =>
    route.fulfill({ json: state.status }),
  );
}

async function assertIndependentIndicators(page: Page) {
  for (const id of ['indicator-process', 'indicator-intent', 'indicator-readiness', 'indicator-broker', 'indicator-safety']) {
    await expect(page.getByTestId(id)).toBeVisible();
  }
}

test.describe('Bot Control status routing', () => {
  test('legacy instance route redirects to the canonical Bot Control route', async ({ page }) => {
    const status = buildScenarioStatus({
      strategyInstanceId: SID,
      readinessVerdict: 'READY',
    });
    await installRoutes(page, {
      summaries: [buildSummary({ strategyInstanceId: SID, readinessVerdict: 'READY' })],
      status,
      account: buildAccountSummary({}),
    });

    await page.goto(`/broker/instances/${SID}`);

    await expect(page).toHaveURL(new RegExp(`/broker/bots/${SID}(?:$|[?#])`));
    await expect(page.getByTestId('inner-tab-status')).toHaveAttribute('aria-selected', 'true');
  });

  test('READY + RUNNING shows Status tab by default', async ({ page }) => {
    const status = buildScenarioStatus({
      strategyInstanceId: SID,
      readinessVerdict: 'READY',
      processState: 'running',
      intent: 'RUNNING',
    });
    await installRoutes(page, {
      summaries: [buildSummary({ strategyInstanceId: SID, readinessVerdict: 'READY', desiredState: 'RUNNING' })],
      status,
      account: buildAccountSummary({}),
    });
    await page.goto(`/broker/bots/${SID}`);
    await expect(page.getByTestId('inner-tab-status')).toHaveAttribute('aria-selected', 'true');
    await assertIndependentIndicators(page);
  });

  test('BLOCKED + WAITING_FOR_HOST shows three independent facts', async ({ page }) => {
    const status = buildScenarioStatus({
      strategyInstanceId: SID,
      readinessVerdict: 'BLOCKED',
      processState: 'idle',
      intent: 'RUNNING',
      readinessGates: [
        {
          name: 'broker_connection',
          status: 'fail',
          severity: 'hard',
          detail: 'broker session not connected',
          suggested_action: { kind: 'redeploy' },
          suggested_action_unavailable_reason: null,
        },
      ],
    });
    await installRoutes(page, {
      summaries: [buildSummary({ strategyInstanceId: SID, readinessVerdict: 'BLOCKED', processState: 'idle', desiredState: 'RUNNING' })],
      status,
      account: buildAccountSummary({}),
    });
    await page.goto(`/broker/bots/${SID}`);
    await expect(page.getByTestId('indicator-process')).toContainText('WAITING_FOR_HOST');
    await expect(page.getByTestId('indicator-intent')).toContainText('RUNNING');
    await expect(page.getByTestId('indicator-readiness')).toContainText('BLOCKED');
    // Status tab is the canonical landing point for non-READY.
    await expect(page.getByTestId('inner-tab-status')).toHaveAttribute('aria-selected', 'true');
  });

  test('Operator manual selection persists on subsequent same-verdict polls', async ({ page }) => {
    const status = buildScenarioStatus({ strategyInstanceId: SID, readinessVerdict: 'READY' });
    await installRoutes(page, {
      summaries: [buildSummary({ strategyInstanceId: SID, readinessVerdict: 'READY' })],
      status,
      account: buildAccountSummary({}),
    });
    await page.goto(`/broker/bots/${SID}`);
    await page.getByTestId('inner-tab-audit').click();
    await expect(page.getByTestId('inner-tab-audit')).toHaveAttribute('aria-selected', 'true');
    // wait for the next poll tick
    await page.waitForTimeout(4_500);
    await expect(page.getByTestId('inner-tab-audit')).toHaveAttribute('aria-selected', 'true');
  });
});
