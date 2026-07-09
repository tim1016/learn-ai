import { expect, test, type Page } from '@playwright/test';

import {
  buildAccountSummary,
  buildActivityProjection,
  buildChartSnapshot,
  buildLifecycleTimeline,
  buildScenarioStatus,
  buildSummary,
} from './fixtures/bot-control-fixtures';
import type { OperatorNotice } from '../../src/app/api/live-instances.types';

const SID = 'dep_val_smoke_001';

function criticalNotice(
  code: OperatorNotice['code'],
  title: string,
  sourceCode: string,
): OperatorNotice {
  return {
    code,
    tier: 'critical',
    title,
    message: `${title}. Operator trust is blocked until this clears.`,
    source_codes: [sourceCode],
    forensic_facts: { source_rank: sourceCode },
    actionability: 'routed',
    resolution: 'Clears when the operator follows the named runbook and fresh evidence proves recovery.',
    remedy_status: null,
    action: { kind: 'open_runbook', label: 'Open runbook', target: 'runtime-freshness' },
    runbook_slug: 'runtime-freshness',
    occurred_at_ms: 1_800_000_000_000,
  };
}

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
    await expect(page.locator('.connection-pill')).toContainText('Broker session disconnected');
    await expect(page.getByTestId('bot-control-attention-toggle')).toHaveCount(0);
    await expect(page.getByTestId('bot-control-attention-panel')).toHaveCount(0);
    const attentionDetails = page.getByLabel('Attention details');
    await expect(attentionDetails).toContainText('Broker proof waits for a live runtime');
    await expect(attentionDetails).toContainText(
      'Start a bot process only after IBKR positions/executions are manually verified',
    );
    await expect(page.locator('[data-testid="bot-status-banner"]')).toHaveCount(0);
    await expect(page.getByTestId('inner-tab-status')).toHaveCount(0);
  });

  test('renders one dominant banner with folded criticals and independent status facts', async ({ page }) => {
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
    const dominant = criticalNotice(
      'runtime.command_loop_unresponsive',
      'Command loop is unresponsive',
      'COMMAND_LOOP_UNRESPONSIVE',
    );
    const folded = criticalNotice(
      'activity.publisher_not_running',
      'Broker activity publisher is not running',
      'PUBLISHER_NOT_RUNNING',
    );
    status.operator_surface.notice_placement = {
      banner: dominant,
      banner_fold_count: 1,
      banner_folded: [folded],
      attention: [],
      quiet_status: [],
    };

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

    await expect(page.getByTestId('bot-control-dominant-notice')).toHaveCount(1);
    await expect(page.getByTestId('bot-control-dominant-notice')).toContainText(
      'Command loop is unresponsive',
    );
    await expect(page.getByTestId('bot-control-dominant-notice-fold')).toContainText(
      '+1 more critical',
    );
    await page.getByText('+1 more critical').click();
    await expect(page.getByTestId('bot-control-dominant-notice-fold')).toContainText(
      'Broker activity publisher is not running',
    );
    await expect(page.getByTestId('bot-run-signal')).toContainText('On');
    await expect(page.locator('.top-action-banner')).toContainText('End day now');
    await expect(page.locator('.posture-pills')).toContainText('Unsafe');
    await expect(page.locator('.posture-pills')).toContainText('Blocked before submit');
    await expect(page.locator('.connection-pill')).toContainText('Broker session disconnected');
    await expect(page.locator('[data-testid="bot-status-banner"]')).toHaveCount(0);
  });
});
