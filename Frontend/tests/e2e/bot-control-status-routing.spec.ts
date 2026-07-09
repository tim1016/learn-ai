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
    await expect(page.locator('#bot-control-title')).toContainText(SID);
    await expect(page.locator('.verdict-card')).toHaveAttribute('data-state', 'On duty');
    await expect(page.getByTestId('verdict-verb')).toHaveText('End day now');
  });

  test('canonical Bot Control page renders the on-duty verdict and activity surface', async ({ page }) => {
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

    await expect(page.locator('.verdict-card')).toHaveAttribute('data-layout', 'strip');
    await expect(page.locator('.vc-state')).toHaveText('On duty');
    await expect(page.getByTestId('verdict-verb')).toHaveText('End day now');
    await expect(page.getByLabel('Bot vitals')).toContainText('Position');
    await expect(page.getByLabel('Bot vitals')).toContainText('Orders today');
    await expect(page.getByTestId('activity-tab')).toBeVisible();
    await expect(page.getByTestId('activity-trade-chart')).toContainText('Trade chart');
    await expect(page.getByTestId('activity-broker-activity')).toBeVisible();
  });

  test('blocked bot surfaces scoped why evidence without restoring old shell tabs', async ({ page }) => {
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

    await expect(page.locator('.verdict-card')).toHaveAttribute('data-state', 'Off duty');
    await expect(page.locator('.vc-state')).toHaveText('Off duty');
    await expect(page.locator('.vc-reason')).toContainText('Run roll call to issue a start offer.');
    await expect(page.getByTestId('verdict-verb')).toHaveText('Reconcile now');
    await page.getByRole('button', { name: 'why?' }).click();
    await expect(page.getByTestId('why-drawer')).toContainText(
      'This bot needs attention before it can submit.',
    );
    await expect(page.getByTestId('why-drawer')).toContainText(/Broker connection/i);
    await expect(page.getByTestId('why-drawer')).toContainText('broker session not connected');
    await expect(page.getByTestId('bot-control-attention-toggle')).toHaveCount(0);
    await expect(page.getByTestId('bot-control-attention-panel')).toHaveCount(0);
    await expect(page.locator('[data-testid="bot-status-banner"]')).toHaveCount(0);
    await expect(page.getByTestId('inner-tab-status')).toHaveCount(0);
  });

  test('renders one verdict card without restoring dominant notice banners', async ({ page }) => {
    const status = buildScenarioStatus({
      strategyInstanceId: SID,
      readinessVerdict: 'BLOCKED',
      processState: 'running',
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
          processState: 'running',
          desiredState: 'RUNNING',
        }),
      ],
      status,
    });

    await page.goto(`/broker/bots/${SID}`);

    await expect(page.locator('.verdict-card')).toHaveCount(1);
    await expect(page.locator('.verdict-card')).toHaveAttribute('data-state', 'On duty');
    await expect(page.locator('.vc-state')).toHaveText('On duty');
    await expect(page.getByTestId('verdict-verb')).toHaveText('End day now');
    await expect(page.getByLabel('Bot vitals')).toContainText('Position');
    await expect(page.getByTestId('activity-tab')).toBeVisible();
    await expect(page.getByTestId('bot-control-dominant-notice')).toHaveCount(0);
    await expect(page.getByTestId('bot-control-dominant-notice-fold')).toHaveCount(0);
    await expect(page.locator('[data-testid="bot-status-banner"]')).toHaveCount(0);
  });
});
