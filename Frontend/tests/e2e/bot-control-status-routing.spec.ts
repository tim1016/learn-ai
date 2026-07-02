import { expect, test, type Page } from '@playwright/test';

import {
  buildAccountSummary,
  buildActivityProjection,
  buildChartSnapshot,
  buildLifecycleTimeline,
  buildScenarioStatus,
  buildSummary,
} from './fixtures/bot-control-fixtures';

const SID = 'dep_val_smoke_001';

async function installBotControlRoutes(
  page: Page,
  state: {
    summaries: ReturnType<typeof buildSummary>[];
    status: ReturnType<typeof buildScenarioStatus>;
    account?: ReturnType<typeof buildAccountSummary>;
  },
): Promise<void> {
  await page.route(/\/api\/live-instances\/account-summary(?:\?.*)?$/, (route) =>
    route.fulfill({ json: state.account ?? buildAccountSummary() }),
  );
  await page.route(/\/api\/live-instances\/[^/]+\/status(?:\?.*)?$/, (route) =>
    route.fulfill({ json: state.status }),
  );
  await page.route(/\/api\/lifecycle-projection\/timeline(?:\?.*)?$/, (route) =>
    route.fulfill({ json: buildLifecycleTimeline(state.status.strategy_instance_id) }),
  );
  await page.route(/\/api\/live-instances\/[^/]+\/activity(?:\?.*)?$/, (route) =>
    route.fulfill({ json: buildActivityProjection(state.status.strategy_instance_id) }),
  );
  await page.route(/\/api\/live-instances\/[^/]+\/chart-snapshot(?:\?.*)?$/, (route) =>
    route.fulfill({ json: buildChartSnapshot(state.status.strategy_instance_id) }),
  );
  await page.route(/\/api\/live-instances\/[^/]+\/active-dates(?:\?.*)?$/, (route) =>
    route.fulfill({ json: [] }),
  );
  await page.route(/\/api\/live-runs\/[^/]+\/incidents(?:\?.*)?$/, (route) =>
    route.fulfill({ json: [] }),
  );
  await page.route(/\/api\/live-instances(?:\?.*)?$/, (route) =>
    route.fulfill({ json: state.summaries }),
  );
}

test.describe('Bot Control route and page shell', () => {
  test('legacy instance route redirects to the canonical Bot Control route', async ({ page }) => {
    const status = buildScenarioStatus({
      strategyInstanceId: SID,
      readinessVerdict: 'READY',
    });
    await installBotControlRoutes(page, {
      summaries: [buildSummary({ strategyInstanceId: SID, readinessVerdict: 'READY' })],
      status,
    });

    await page.goto(`/broker/instances/${SID}`);

    await expect(page).toHaveURL(new RegExp(`/broker/bots/${SID}(?:$|[?#])`));
    await expect(page.locator('#bot-control-title')).toHaveText(SID);
    await expect(page.getByLabel('Bot lifecycle chart')).toBeVisible();
  });

  test('canonical Bot Control page renders lifecycle and recent activity workbench', async ({ page }) => {
    const status = buildScenarioStatus({
      strategyInstanceId: SID,
      readinessVerdict: 'READY',
      processState: 'running',
      intent: 'RUNNING',
    });
    await installBotControlRoutes(page, {
      summaries: [buildSummary({ strategyInstanceId: SID, readinessVerdict: 'READY', desiredState: 'RUNNING' })],
      status,
    });

    await page.goto(`/broker/bots/${SID}`);

    await expect(page.getByRole('heading', { name: 'Lifecycle overview' })).toBeVisible();
    await expect(page.getByRole('region', { name: 'Lifecycle controls' })).toBeVisible();
    await expect(page.getByTestId('bot-control-context-header')).toContainText('Deploy or start');
    await expect(page.getByTestId('bot-control-workbench-tabs')).toContainText('Recent activity');
    await expect(page.getByTestId('bot-control-workbench-tabs')).toContainText('Full audit trail');
    await expect(page.getByTestId('bot-control-recent-activity')).toContainText(
      'Broker acknowledgment failed; submit outcome is uncertain.',
    );
    await expect(page.getByTestId('activity-tab')).toBeVisible();
  });

  test('blocked bot surfaces independent posture facts without restoring old shell tabs', async ({ page }) => {
    const status = buildScenarioStatus({
      strategyInstanceId: SID,
      readinessVerdict: 'BLOCKED',
      processState: 'idle',
      intent: 'RUNNING',
      brokerSafety: 'UNSAFE',
      brokerConnection: 'DISCONNECTED',
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
    await installBotControlRoutes(page, {
      summaries: [
        buildSummary({
          strategyInstanceId: SID,
          readinessVerdict: 'BLOCKED',
          processState: 'idle',
          desiredState: 'RUNNING',
        }),
      ],
      status,
    });

    await page.goto(`/broker/bots/${SID}`);

    await expect(page.locator('.posture-pills')).toContainText('Broker proof');
    await expect(page.locator('.posture-pills')).toContainText('Unsafe');
    await expect(page.locator('.posture-pills')).toContainText('Blocked before submit');
    await expect(page.locator('.connection-pill')).toContainText('Disconnected');
    await page.getByTestId('bot-control-attention-toggle').click();
    await expect(page.getByTestId('bot-control-attention-panel')).toContainText('Broker session is disconnected');
    await expect(page.locator('[data-testid="bot-status-banner"]')).toHaveCount(0);
    await expect(page.getByTestId('inner-tab-status')).toHaveCount(0);
  });
});
