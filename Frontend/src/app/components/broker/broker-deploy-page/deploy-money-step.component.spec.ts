import { fireEvent, render, screen, within } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { resourceTarget } from '../../../fleet/resource-target';
import { fakeAccountMoney } from '../../../testing/account-money-fixtures';
import {
  BrokerV2PanelService,
  type AccountMoneyView,
  type DeployBotBody,
  type DeploymentBudgetPreview,
} from '../v2-panel/lib/broker-v2-panel.service';
import { DeployMoneyStepComponent, type MoneyReview } from './deploy-money-step.component';

const TARGET = resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA9', bindingGeneration: 3, routingEpoch: 4 });
const BODY: DeployBotBody = {
  strategy_key: 'deployment_validation', symbol: 'SPY',
  execution_mode: 'paper', sizing: { preset: 'safe_canary', quantity: 1 }, carryover_policy: 'FORBID',
  exit_terms: { exit_allowance_bps: 10, band_multiple: 2, spread_cap_bps: 10 }, parameters: {},
};

/** The account as it stands, and with a $617.28 NEW slice carved from free —
 * exactly as the backend would author both (no arithmetic here either). */
const MONEY_NOW = fakeAccountMoney();
const MONEY_AFTER: AccountMoneyView = fakeAccountMoney({
  free_to_deploy_usd: '97712.29',
  segments: [
    ...(MONEY_NOW.segments ?? []).filter((segment) => segment.kind !== 'free'),
    { kind: 'new', label: 'new bot', amount_usd: '617.28', share_bps: 62 },
    { kind: 'free', label: 'free to deploy', amount_usd: '97712.29', share_bps: 9770 },
  ],
});

const FACTS: DeploymentBudgetPreview = {
  state: 'ready', detail: 'This is an entry-admission budget. Market fills and losses can exceed it.',
  world: 'real_paper', custody_account_id: 'PA9', risk_revision: 4,
  unreserved_usd: '98329.57', minimum_budget_usd: '501.02', estimated_price_usd: '501.01',
  shortcuts: [{ key: 'half', label: '50%', amount_usd: '617.28', explanation: '50% of $1234.56 currently unreserved cash.' }],
  review_token: null, confirmation_text: null, observed_at_ms: 1_800_000_000_000,
  bot_name_note: 'Named at Deploy: spy-dv-YYYYMMDD-HHMM, from the symbol, strategy and New York minute.',
  money_after: MONEY_NOW,
};

function reviewed(amount: string, overrides: Partial<DeploymentBudgetPreview> = {}): DeploymentBudgetPreview {
  return { ...FACTS, review_token: `review-${amount}`, budget_usd: amount, money_after: MONEY_AFTER, ...overrides };
}

interface Deferred<T> { readonly promise: Promise<T>; resolve(value: T): void }
function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  return { promise: new Promise<T>((done) => { resolve = done; }), resolve };
}

async function setup(previewBudget = vi.fn().mockImplementation(async (_target, body: DeployBotBody) =>
  body.budget ? reviewed(body.budget.amount_usd) : FACTS)) {
  const emitted: (MoneyReview | null)[] = [];
  const result = await render(DeployMoneyStepComponent, {
    inputs: { target: TARGET, body: BODY },
    on: { reviewed: (review: MoneyReview | null) => emitted.push(review) },
    providers: [{ provide: BrokerV2PanelService, useValue: { previewBudget } }],
  });
  await screen.findByText('Minimum for one position');
  return { ...result, previewBudget, emitted };
}

function legend(): HTMLElement {
  return screen.getByRole('list', { name: /Where the money is now|Account money after this Deploy/ });
}

describe('Deploy step 3, Money', () => {
  afterEach(() => vi.useRealTimers());

  it('draws the account as it stands, then a NEW slice only once the typed amount’s preview answers', async () => {
    const answer = deferred<DeploymentBudgetPreview>();
    const previewBudget = vi.fn().mockImplementation((_target, body: DeployBotBody) =>
      body.budget ? answer.promise : Promise.resolve(FACTS));
    const { emitted } = await setup(previewBudget);
    expect(within(legend()).getByText('free to deploy')).toBeTruthy();
    expect(screen.getByText('Free to deploy').nextElementSibling?.textContent).toBe('$98,329.57');

    fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: '617.28' } });
    await vi.waitFor(() => expect(previewBudget).toHaveBeenCalledTimes(2));
    expect(previewBudget.mock.calls[1][1].budget).toEqual({ amount_usd: '617.28', risk_revision: 4 });
    // Asked, not answered: the bar still shows the account as it stands.
    expect(within(legend()).queryByText('new bot')).toBeNull();
    expect(screen.getByRole('status').textContent).toContain('Previewing this amount');

    answer.resolve(reviewed('617.28'));
    const after = await screen.findByRole('list', { name: 'Account money after this Deploy' });
    expect(within(after).getByText('new bot')).toBeTruthy();
    expect(within(after).getByText('$617.28')).toBeTruthy();
    expect(screen.getByText(/would be set aside for this bot/).textContent).toContain('$617.28');
    await vi.waitFor(() => expect(emitted.at(-1)?.preview.review_token).toBe('review-617.28'));
    expect(emitted.at(-1)?.amount).toBe('617.28');
  });

  it('takes a server shortcut exactly as authored and previews it at once', async () => {
    const { previewBudget } = await setup();

    fireEvent.click(screen.getByRole('button', { name: '50% · $617.28' }));

    expect((screen.getByLabelText('Dollar budget (USD)') as HTMLInputElement).value).toBe('617.28');
    await vi.waitFor(() => expect(previewBudget.mock.calls.at(-1)?.[1].budget?.amount_usd).toBe('617.28'));
    expect(await screen.findByText(/would be set aside for this bot/)).toBeTruthy();
  });

  it('shows a refused amount’s own sentence, with the backend’s exact amounts, and emits no review', async () => {
    const previewBudget = vi.fn().mockImplementation(async (_target, body: DeployBotBody) =>
      body.budget
        ? { state: 'unavailable', world: 'real_paper', custody_account_id: 'PA9',
          detail: 'One estimated position needs at least $501.02, including fees.' } satisfies DeploymentBudgetPreview
        : FACTS);
    const { emitted } = await setup(previewBudget);

    fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: '100.00' } });

    expect(await screen.findByText('One estimated position needs at least $501.02, including fees.')).toBeTruthy();
    expect(emitted.every((review) => review === null)).toBe(true);
    // A refused amount carries no bar of its own here: the account as it stands stays drawn.
    expect(within(legend()).queryByText('new bot')).toBeNull();
  });

  it('never lets a late answer for an earlier amount stand for the amount on screen', async () => {
    const first = deferred<DeploymentBudgetPreview>();
    const previewBudget = vi.fn().mockImplementation((_target, body: DeployBotBody) => {
      if (!body.budget) return Promise.resolve(FACTS);
      return body.budget.amount_usd === '617.28' ? first.promise : Promise.resolve(reviewed(body.budget.amount_usd));
    });
    const { emitted } = await setup(previewBudget);

    fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: '617.28' } });
    await vi.waitFor(() => expect(previewBudget).toHaveBeenCalledTimes(2));
    fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: '700.00' } });
    first.resolve(reviewed('617.28'));

    await vi.waitFor(() => expect(emitted.at(-1)?.amount).toBe('700.00'));
    expect(emitted.some((review) => review?.amount === '617.28')).toBe(false);
  });

  it('words Dry Run money as simulated starting cash, with no account bar and no free to deploy (H18)', async () => {
    const dryRunFacts: DeploymentBudgetPreview = {
      ...FACTS, world: 'synthetic', custody_account_id: null, unreserved_usd: null, risk_revision: 0,
      money_after: null, shortcuts: [FACTS.shortcuts?.[0] ?? { key: 'position_headroom', label: '1.2 × one position',
        amount_usd: '601.23', explanation: '1.2 × the $501.02 estimated position.' }],
    };
    await render(DeployMoneyStepComponent, {
      inputs: { target: TARGET, body: { ...BODY, execution_mode: 'dry_run' } },
      providers: [{ provide: BrokerV2PanelService, useValue: { previewBudget: vi.fn().mockResolvedValue(dryRunFacts) } }],
    });
    await screen.findByText('Minimum for one position');

    expect(screen.getByLabelText('Simulated starting cash (USD)')).toBeTruthy();
    expect(screen.queryByText('Free to deploy')).toBeNull();
    expect(screen.queryByText('Unreserved cash')).toBeNull();
    expect(screen.queryByText('Unknown')).toBeNull();
    expect(screen.queryByText('PA9')).toBeNull();
    expect(screen.queryByRole('list', { name: /money/i })).toBeNull();
  });

  it('asks for What and How first while the settings are incomplete', async () => {
    const previewBudget = vi.fn();
    await render(DeployMoneyStepComponent, {
      inputs: { target: TARGET, body: null },
      providers: [{ provide: BrokerV2PanelService, useValue: { previewBudget } }],
    });

    expect(screen.getByText('Finish What and How first: the budget depends on both.')).toBeTruthy();
    expect(previewBudget).not.toHaveBeenCalled();
  });

  it('re-checks a price wait on its own, bounded, and leaves the manual refresh', async () => {
    vi.useFakeTimers();
    const priceWait: DeploymentBudgetPreview = {
      state: 'awaiting_price', detail: 'Wait for a fresh IBKR price for this instrument, then review the budget.',
      world: 'real_paper', custody_account_id: 'PA9',
    };
    const previewBudget = vi.fn().mockResolvedValue(priceWait);
    await render(DeployMoneyStepComponent, {
      inputs: { target: TARGET, body: BODY },
      providers: [{ provide: BrokerV2PanelService, useValue: { previewBudget } }],
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.getByText(priceWait.detail)).toBeTruthy();

    await vi.advanceTimersByTimeAsync(10 * 60_000);
    const bounded = previewBudget.mock.calls.length;
    expect(bounded).toBeGreaterThan(1);
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(previewBudget).toHaveBeenCalledTimes(bounded);

    fireEvent.click(screen.getByRole('button', { name: 'Refresh money' }));
    await vi.advanceTimersByTimeAsync(0);
    expect(previewBudget).toHaveBeenCalledTimes(bounded + 1);
  });

  it('asks for a well-formed dollar amount before previewing it', async () => {
    const { previewBudget } = await setup();
    const amount = screen.getByLabelText('Dollar budget (USD)');

    fireEvent.input(amount, { target: { value: '12.345' } });
    fireEvent.blur(amount);

    expect(await screen.findByText('Enter a positive dollar amount with at most two decimal places.')).toBeTruthy();
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(previewBudget).toHaveBeenCalledTimes(1);
  });
});
