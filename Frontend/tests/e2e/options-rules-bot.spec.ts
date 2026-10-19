import { expect, test, type Page } from '@playwright/test';

import type { components } from '../../src/app/api/broker.types';
import { OPTION_CATALOGUE } from '../../src/app/components/broker/broker-deploy-page/option-structure/option-structure.fixtures';
import { previewAnswer } from '../../src/app/components/options-backtest/position/testing';
import { fakeBotPanelView, fakeChartFeed } from '../../src/app/testing/bot-panel-fixtures';
import { OwnerWalkWorld, PAPER_ACCOUNT, PAPER_API, PAPER_WORKSPACE, WALKED_BOT } from './support/owner-walk-world';

type Schemas = components['schemas'];
const GOLDEN: Schemas['GoldenOptionSetupView'] = {
  id: 'ogs_0123456789abcdef0123456789abcdef', name: 'Tested SPY setup', underlying: 'SPY',
  setup: { version: 1, underlying: 'SPY', entry: { anchor: 'open', offset_ms: 300000 },
    exit: { anchor: 'close', offset_ms: 2760000 }, square_off: 'partial',
    legs: [{ leg_id: 'call', side: 'buy', right: 'call', contracts: 1,
      expiry: { kind: 'this_week' }, strike: { kind: 'atm' }, stop_loss: { kind: 'percent', value: '25' },
      momentum: { kind: 'percent_up', value: '25' }, reentry_on_stop: { kind: 'at_once', count: 1 } }],
    slippage_percent: '0.3', price_step_usd: '0.01', fees: { kind: 'not_charged' }, starting_cash_usd: '10000' },
  setup_sha256: 'a'.repeat(64), source_run_id: 7, rules_digest: 'b'.repeat(64), proof_sha256: 'c'.repeat(64),
  status: 'ready', status_explanation: 'This exact setup is approved.', created_at_ms: 1791828000000,
  approved_by: 'owner', note: 'Reviewed the selected completed backtest.', proof: {}, events: [],
  evidence: { first_session_open_ms: 1791379800000, last_session_open_ms: 1791552600000,
    sessions_tested: 3, trades: 4, net_profit_usd: '25.00', starting_cash_usd: '10000',
    slippage_percent: '0.3', limitations: ['Trade bars do not prove live fills.'] },
};
const CONFIGURATION: Schemas['OptionsBotConfiguration'] = {
  version: 1, golden_setup_id: GOLDEN.id, setup_sha256: GOLDEN.setup_sha256,
  rules_digest: GOLDEN.rules_digest, setup: GOLDEN.setup,
};

async function optionsWorld(page: Page, loseAnswer = false) {
  const world = new OwnerWalkWorld();
  await world.install(page);
  const deployments: Schemas['OptionsDeploySubmission'][] = [];
  const resumes: Schemas['OptionsResumeRequest'][] = [];
  let interrupted = false;
  const receipt: Schemas['BudgetDeployCommandReceipt'] = {
    status: 'deployed', outcome: 'success', receipt_id: 'options-command', command_id: 'options-command',
    recorded_at_ms: GOLDEN.created_at_ms, first_deployed_at_ms: GOLDEN.created_at_ms,
    strategy_instance_id: WALKED_BOT, run_id: 'options-run', account_id: PAPER_ACCOUNT,
    world: 'real_paper', committed_usd: '10000.00', message: 'Options bot is deployed',
    explanation: 'The approved setup is on duty.', next_action: 'Open the bot.',
  };
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/research/golden-option-setups') {
      await route.fulfill({ json: [GOLDEN] });
    } else if (path === '/api/research/options-backtest/setups/describe') {
      const summary: Schemas['SetupDescriptionResponse'] = {
        setup: GOLDEN.setup, window: ['The setup follows its approved trading session.'],
        legs: [{ leg_id: 'call', sentences: ['Buys one call with its approved stop and re-entry.'] }],
        square_off: ['Closes at the approved daily exit.'], costs: [], notices: [],
      };
      await route.fulfill({ json: summary });
    } else if (path === '/api/research/options-backtest/position-preview') {
      await route.fulfill({ json: previewAnswer() });
    } else if (path === `${PAPER_API}/bots/option-structures`) {
      await route.fulfill({ json: OPTION_CATALOGUE });
    } else if (path === `${PAPER_API}/options-bots/review`) {
      const body: Schemas['OptionsDeployRequest'] = request.postDataJSON();
      const review: Schemas['OptionsDeploymentReview'] = {
        golden: GOLDEN, allowed: body.budget != null, explanation: 'The exact setup is approved for this account.',
        first_eligible_session_open_ms: GOLDEN.evidence.last_session_open_ms,
        rules_summary: ['Daily session rules and tested contract quantities are fixed.'],
        budget: { state: 'ready', world: 'real_paper', detail: 'Future entries need current Clerk admission.',
          risk_revision: 8, budget_usd: body.budget?.amount_usd ?? null,
          review_token: body.budget ? 'review-options-8' : null, shortcuts: [] },
      };
      await route.fulfill({ json: review });
    } else if (path === `${PAPER_API}/options-bots/deploy`) {
      deployments.push(request.postDataJSON());
      world.phase = 'running';
      if (loseAnswer && deployments.length === 1) await route.abort('failed');
      else await route.fulfill({ json: receipt });
    } else if (path.startsWith(`${PAPER_API}/options-bots/deploy-submissions/`)) {
      await route.fulfill({ json: receipt });
    } else if (path === `${PAPER_API}/bots/${WALKED_BOT}/panel` || path === `${PAPER_API}/bots/${WALKED_BOT}/live-snapshot`) {
      const panel = fakeBotPanelView({
        strategy_instance_id: WALKED_BOT, account_id: PAPER_ACCOUNT, symbol: 'SPY',
        bot_kind: 'options', options_bot: CONFIGURATION,
      });
      const snapshot: Schemas['BotPanelLiveSnapshot'] = {
        panel, stream_epoch: 'options-browser', surface_version: interrupted ? 2 : 3,
        live_chart: { as_of_ms: GOLDEN.created_at_ms, bars: [], feed: fakeChartFeed(),
          fill_markers: [], overlay_notices: [], resolution: '5s', strategy_instance_id: WALKED_BOT, symbol: 'SPY',
          trading_date_open_ms: GOLDEN.evidence.last_session_open_ms, trading_date_close_ms: GOLDEN.created_at_ms },
      };
      await route.fulfill({ json: path.endsWith('/live-snapshot') ? snapshot : panel });
    } else if (path === `${PAPER_API}/options-bots/${WALKED_BOT}/session`) {
      const session: Schemas['OptionsBotSessionView'] = {
        strategy_instance_id: WALKED_BOT, golden_setup_id: GOLDEN.id,
        session_open_ms: GOLDEN.evidence.last_session_open_ms, last_minute_ms: GOLDEN.created_at_ms,
        pending_decisions: 0, entries_enabled: !interrupted, resume_allowed: interrupted,
        resume_explanation: interrupted ? 'Resume the interrupted session from its recorded state.' : 'The session is on duty.',
        legs: [{ leg_id: 'call', side: 'buy', phase: 'held', entries_filled: 1, reentries_left: 1,
          held_contract: 'SPY261016C00500000', held_contracts: 1, average_fill_price: '2.50', stop_level: '2.00' }], events: [],
      };
      await route.fulfill({ json: session });
    } else if (path === `${PAPER_API}/options-bots/${WALKED_BOT}/resume`) {
      resumes.push(request.postDataJSON());
      interrupted = false;
      await route.fulfill({ json: { running: true } });
    } else await route.fallback();
  });
  return { world, deployments, resumes, interrupt: () => { interrupted = true; } };
}

async function reviewOptions(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Options bot', exact: true }).click();
  await page.getByLabel('Approved tested version').selectOption(GOLDEN.id);
  await expect(page.getByRole('heading', { name: 'Historical position payoff' })).toBeVisible();
  await expect(page.getByText('Underlying $500.00')).toBeVisible();
  await expect(page.getByText('SPY261009C00500000')).toBeVisible();
  await expect(page.getByLabel('Dollar budget (USD)')).toHaveValue('10000');
  await page.getByRole('button', { name: 'Review setup and budget' }).click();
  await expect(page.getByRole('button', { name: 'Deploy Options bot', exact: true })).toBeEnabled();
}

test('deploys an exact golden Options setup and shows confirmed leg state on its bot page', async ({ page }) => {
  const state = await optionsWorld(page);
  await page.goto(`${PAPER_WORKSPACE}/deploy`);
  await reviewOptions(page);
  await page.getByRole('button', { name: 'Deploy Options bot', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Options bot is deployed' })).toBeVisible();
  expect(state.deployments).toHaveLength(1);
  expect(state.deployments[0]).toMatchObject({ golden_setup_id: GOLDEN.id, execution_mode: 'paper',
    budget: { amount_usd: '10000', risk_revision: 8, review_token: 'review-options-8' } });
  await page.goto(`${PAPER_WORKSPACE}/bots/${WALKED_BOT}`);
  await expect(page.getByRole('heading', { name: 'Options bot session' })).toBeVisible();
  await expect(page.getByText('Average confirmed fill: $2.50')).toBeVisible();
  await expect(page.getByText('Re-entries left: 1', { exact: false })).toBeVisible();
  expect(state.world.unexpected()).toEqual([]);
});

test('recovers a lost deployment response after reload without sending another deploy', async ({ page }) => {
  const state = await optionsWorld(page, true);
  await page.goto(`${PAPER_WORKSPACE}/deploy`);
  await reviewOptions(page);
  await page.getByRole('button', { name: 'Deploy Options bot', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Check deployment status' })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Deploy an Options bot' })).toBeVisible();
  await page.getByRole('button', { name: 'Check deployment status' }).click();
  await expect(page.getByRole('heading', { name: 'Options bot is deployed' })).toBeVisible();
  expect(state.deployments).toHaveLength(1);
  expect(state.world.unexpected()).toEqual([]);
});

test('resumes interrupted position management with no new entry consent', async ({ page }) => {
  const state = await optionsWorld(page);
  state.world.phase = 'running';
  state.interrupt();
  await page.goto(`${PAPER_WORKSPACE}/bots/${WALKED_BOT}`);
  await page.getByRole('button', { name: 'Resume position management', exact: true }).click();
  await expect(page.getByText('Position management resumed. New entries and re-entries remain disabled.')).toBeVisible();
  expect(state.resumes).toHaveLength(1);
  expect(state.resumes[0]?.budget).toBeNull();
  expect(state.world.unexpected()).toEqual([]);
});
