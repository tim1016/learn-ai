import { HttpErrorResponse } from '@angular/common/http';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { resourceTarget } from '../../../fleet/resource-target';
import { fakeAccountMoney } from '../../../testing/account-money-fixtures';
import {
  BrokerV2PanelService,
  type AccountMoneyView,
  type DeployBotBody,
  type DeploymentBudgetPreview,
  type FundingView,
} from '../v2-panel/lib/broker-v2-panel.service';
import { DeployMoneyStepComponent, type MoneyReview } from './deploy-money-step.component';
import { OPTION_REVIEW_PRICED, OPTION_REVIEW_WITH_BUDGET } from './option-deploy.fixtures';

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

  it('keeps each shortcut’s explanation in view where the steps stack, and as its description where they stand as columns', async () => {
    const explanation = '50% of $1234.56 currently unreserved cash.';
    const helpOf = () => {
      const shortcut = screen.getByRole('button', { name: '50% · $617.28' });
      return document.getElementById(shortcut.getAttribute('aria-describedby') ?? '');
    };

    const { fixture } = await setup();
    expect(helpOf()?.textContent).toBe(explanation);
    expect(helpOf()?.classList.contains('sr-only')).toBe(false);

    fixture.componentRef.setInput('compact', true);
    await fixture.whenStable();
    expect(helpOf()?.textContent).toBe(explanation);
    expect(helpOf()?.classList.contains('sr-only')).toBe(true);
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

  it('draws a refused amount’s bar as today’s money, never as money after this Deploy', async () => {
    const previewBudget = vi.fn().mockImplementation(async (_target, body: DeployBotBody) =>
      body.budget
        ? { ...FACTS, state: 'unavailable', review_token: null, budget_usd: null, money_after: MONEY_NOW,
          detail: 'Only $98329.57 is unreserved. Choose a smaller budget or resolve existing claims.' } satisfies DeploymentBudgetPreview
        : FACTS);
    const { emitted } = await setup(previewBudget);

    fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: '99999.00' } });

    const alert = await screen.findByText('Only $98329.57 is unreserved. Choose a smaller budget or resolve existing claims.');
    expect(alert.getAttribute('role')).toBe('alert');
    expect(screen.getByRole('list', { name: 'Where the money is now' })).toBeTruthy();
    expect(screen.queryByRole('list', { name: 'Account money after this Deploy' })).toBeNull();
    expect(emitted.every((review) => review === null)).toBe(true);
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

  it.each([
    ['awaiting_price', 'Wait for a fresh IBKR price for this instrument, then review the budget.'],
    ['unavailable', 'The IBKR market-data feed is unavailable, so this instrument cannot be priced. '
      + 'Restore the connection, then review the budget.'],
  ] as const)('sends no amount preview while the money read is %s, so no review can carry risk revision 0 (#2559)', async (state, detail) => {
    vi.useFakeTimers();
    const wait: DeploymentBudgetPreview = { state, detail, world: 'real_paper', custody_account_id: 'PA9' };
    const previewBudget = vi.fn().mockResolvedValue(wait);
    const emitted: (MoneyReview | null)[] = [];
    await render(DeployMoneyStepComponent, {
      inputs: { target: TARGET, body: BODY },
      on: { reviewed: (review: MoneyReview | null) => emitted.push(review) },
      providers: [{ provide: BrokerV2PanelService, useValue: { previewBudget } }],
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.getByText(detail)).toBeTruthy();

    fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: '617.28' } });
    // Well past the typing settle; short of the first price re-check.
    await vi.advanceTimersByTimeAsync(1_000);

    // The wait has no risk revision to consent to, so the typed amount is never
    // previewed: no request can carry `risk_revision: 0` and read back as
    // "Risk limits changed".
    expect(previewBudget).toHaveBeenCalled();
    expect(previewBudget.mock.calls.every(([, body]) => (body as DeployBotBody).budget === undefined)).toBe(true);
    expect(emitted.every((review) => review === null)).toBe(true);
    expect(screen.queryByText('Previewing this amount…')).toBeNull();
  });

  it('shows the server’s own sentence when the account’s money cannot be read', async () => {
    const previewBudget = vi.fn().mockRejectedValue(new HttpErrorResponse({
      status: 409, error: { detail: { message: 'The account’s holds changed; refresh the account first.' } },
    }));
    await render(DeployMoneyStepComponent, {
      inputs: { target: TARGET, body: BODY },
      providers: [{ provide: BrokerV2PanelService, useValue: { previewBudget } }],
    });

    expect((await screen.findByRole('alert')).textContent).toContain('The account’s holds changed; refresh the account first.');
  });

  it('shows the server’s own sentence when an amount cannot be previewed', async () => {
    const previewBudget = vi.fn().mockImplementation(async (_target, body: DeployBotBody) => {
      if (!body.budget) return FACTS;
      throw new HttpErrorResponse({ status: 409, error: { detail: { message: 'The risk revision changed while you typed.' } } });
    });
    await setup(previewBudget);

    fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: '617.28' } });

    expect(await screen.findByText('The risk revision changed while you typed.')).toBeTruthy();
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

/** The account's side as the backend answers it for a $900.00 budget on $700.00 unreserved and an $800.00 entry. */
const READ_AT = 1_800_000_000_000;
const COMPETES = 'Every bot competes for the same account headroom, so these counts are never added up across bots.';
const OVER_THE_ACCOUNT: FundingView = {
  free_budget: {
    state: 'known', amount_usd: '900.00', source: 'The budget typed in Deploy', as_of_ms: READ_AT,
    terms: [{ label: 'Budget typed in Deploy', amount_usd: '900.00' }],
  },
  account_headroom: {
    state: 'known', amount_usd: '700.00', source: 'Clerk books and the account reading', as_of_ms: READ_AT,
    terms: [{ label: 'Cash in the account', amount_usd: '1000.00' }, { label: 'Set aside for running bots', amount_usd: '-300.00' }],
  },
  broker_buying_power: {
    state: 'known', amount_usd: '5000.00', source: "Alpaca's account answer", as_of_ms: READ_AT,
    terms: [{ label: 'Option buying power', amount_usd: '5000.00' }],
  },
  need: {
    commitment_usd: '800.00',
    terms: [{ label: 'Broker requirement and net premium', amount_usd: '799.91' }, { label: 'Fee provision', amount_usd: '0.09' }],
  },
  admits: false, binding: 'account_headroom', shortfall_usd: '200.00', fits: 0,
  fits_sentence: `No whole entry fits right now under the account's headroom, the limit that refuses this budget. ${COMPETES}`,
  sentence: 'This budget is $900.00, but the account has only $700.00 unreserved. Shortfall: $200.00.',
  action: "Choose a smaller budget, free another bot's budget, or add cash to the account.",
};

/** The same account with a $1,000.00 budget: it passes, one entry fits, and Alpaca's reading is stale. */
const COVERED: FundingView = {
  ...OVER_THE_ACCOUNT,
  free_budget: { ...OVER_THE_ACCOUNT.free_budget, amount_usd: '1000.00', terms: [{ label: 'Budget typed in Deploy', amount_usd: '1000.00' }] },
  account_headroom: { ...OVER_THE_ACCOUNT.account_headroom, amount_usd: '5000.00' },
  broker_buying_power: {
    state: 'stale', amount_usd: '5000.00', source: "Alpaca's account answer", as_of_ms: READ_AT - 46_000,
    terms: [{ label: 'Option buying power', amount_usd: '5000.00' }],
  },
  admits: true, binding: 'bot_budget', shortfall_usd: null, fits: 1, action: null,
  fits_sentence: `One whole entry fits right now under the bot's budget, the limit with the least room. ${COMPETES}`,
  sentence: "This budget is $1,000.00 and the account has $5,000.00 unreserved, enough for the entry's $800.00 including its fee provision. "
    + 'Passing our own limits does not guarantee that Alpaca accepts the order.',
};

function limitRows(): { label: string; amount: string | null; state: string | null; mark: string | null }[] {
  const text = (row: Element, selector: string) => row.querySelector(selector)?.textContent?.trim() ?? null;
  return Array.from(document.querySelectorAll('.funding-answer__limit')).map((row) => ({
    label: text(row, '.funding-answer__label') ?? '',
    amount: text(row, '.funding-answer__amount'),
    state: text(row, '.funding-answer__state'),
    mark: text(row, '.funding-answer__mark'),
  }));
}

function occurrences(text: string): number {
  return (document.body.textContent ?? '').split(text).length - 1;
}

describe('Deploy step 3, Money: the funding answer beside the budget (#3099)', () => {
  it('shows a refused budget’s limits with the binding one marked, the shortfall, what to do and its sentence once', async () => {
    const previewBudget = vi.fn().mockImplementation(async (_target, body: DeployBotBody) =>
      body.budget
        ? { ...FACTS, state: 'unavailable', review_token: null, budget_usd: null, detail: OVER_THE_ACCOUNT.sentence, funding: OVER_THE_ACCOUNT }
        : FACTS);
    await setup(previewBudget);

    fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: '900.00' } });

    await vi.waitFor(() => expect(limitRows().length).toBe(3));
    expect(limitRows()).toEqual([
      { label: 'Free budget', amount: '$900.00', state: null, mark: null },
      { label: 'Account headroom', amount: '$700.00', state: null, mark: 'binds' },
      { label: 'Alpaca option buying power', amount: '$5,000.00', state: null, mark: null },
    ]);
    expect(occurrences(OVER_THE_ACCOUNT.sentence)).toBe(1);
    expect(screen.getByText(/Choose a smaller budget, free another bot's budget, or add cash to the account\./)).toBeTruthy();
    expect(screen.getByText(/Short by/).textContent?.replace(/\s+/g, ' ').trim()).toBe('Short by $200.00');
  });

  it('says that a budget which passes is no promise, shows how many fit, and says every bot competes for the headroom', async () => {
    const previewBudget = vi.fn().mockImplementation(async (_target, body: DeployBotBody) =>
      body.budget ? reviewed(body.budget.amount_usd, { detail: 'Ready.', funding: COVERED }) : FACTS);
    await setup(previewBudget);

    fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: '1000.00' } });

    expect(await screen.findByText(/Passing our own limits does not guarantee that Alpaca accepts the order\./)).toBeTruthy();
    expect(occurrences(COVERED.sentence)).toBe(1);
    expect(occurrences(COVERED.fits_sentence ?? '')).toBe(1);
    expect(limitRows().map((row) => [row.label, row.mark])).toEqual([
      ['Free budget', 'least room'], ['Account headroom', null], ['Alpaca option buying power', null],
    ]);
    expect(screen.queryByText(/Short by/)).toBeNull();
  });

  it('says a stale buying-power reading is stale with the time it was read, and never as a limit that passes', async () => {
    const previewBudget = vi.fn().mockImplementation(async (_target, body: DeployBotBody) =>
      body.budget ? reviewed(body.budget.amount_usd, { funding: COVERED }) : FACTS);
    await setup(previewBudget);

    fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: '1000.00' } });

    await vi.waitFor(() => expect(limitRows().length).toBe(3));
    const stale = document.querySelectorAll('.funding-answer__limit')[2];
    expect(stale.getAttribute('data-state')).toBe('stale');
    expect(stale.querySelector('.funding-answer__state')?.textContent?.trim()).toBe('stale, as last read');
    expect(stale.querySelector('.funding-answer__time')?.textContent?.trim()).not.toBe('');
    expect(stale.querySelector('.funding-answer__mark')).toBeNull();
  });

  it('says an unknown buying-power reading is unknown, with no amount and no zero', async () => {
    const unknown: FundingView = {
      ...COVERED,
      broker_buying_power: {
        state: 'unknown', source: "Alpaca's account answer, which did not carry option buying power",
        as_of_ms: READ_AT, terms: [],
      },
    };
    const previewBudget = vi.fn().mockImplementation(async (_target, body: DeployBotBody) =>
      body.budget ? reviewed(body.budget.amount_usd, { funding: unknown }) : FACTS);
    await setup(previewBudget);

    fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: '1000.00' } });

    await vi.waitFor(() => expect(limitRows().length).toBe(3));
    expect(limitRows()[2]).toEqual({ label: 'Alpaca option buying power', amount: null, state: 'unknown', mark: null });
  });

  it('shows the account’s side before any amount is typed, with the budget unknown and never zero', async () => {
    const noAmount: FundingView = {
      ...COVERED,
      free_budget: { state: 'unknown', source: 'The budget typed in Deploy, which has no amount yet', terms: [] },
      admits: null, binding: 'account_headroom', shortfall_usd: null, fits: 6,
      sentence: 'The account has $5,000.00 unreserved. The entry needs $800.00 including its fee provision.',
      fits_sentence: `6 whole entries fit right now under the account's headroom, the limit with the least room. ${COMPETES}`,
    };
    await setup(vi.fn().mockResolvedValue({ ...FACTS, funding: noAmount }));

    await vi.waitFor(() => expect(limitRows().length).toBe(3));
    expect(limitRows()[0]).toEqual({ label: 'Free budget', amount: null, state: 'unknown', mark: null });
    expect(screen.getByText(/The account has \$5,000\.00 unreserved\./)).toBeTruthy();
    expect(screen.getByText(/6 whole entries fit right now/)).toBeTruthy();
  });
});

describe('Deploy step 3, Money: what one more structure changes (#3099)', () => {
  const optionFacts = (review: DeploymentBudgetPreview['option_review']): DeploymentBudgetPreview => ({
    ...FACTS, minimum_budget_usd: '824.09', estimated_price_usd: null, option_review: review,
  });

  async function renderOptions(preview: DeploymentBudgetPreview) {
    await render(DeployMoneyStepComponent, {
      inputs: { target: TARGET, body: BODY },
      providers: [{ provide: BrokerV2PanelService, useValue: { previewBudget: vi.fn().mockResolvedValue(preview) } }],
    });
    await screen.findByText('Set aside for 2 structures');
  }

  function rows(): { label: string; text: string }[] {
    return Array.from(document.querySelectorAll('.option-money__rows > div')).map((row) => ({
      label: row.querySelector('dt')?.textContent?.trim() ?? '',
      text: (row.querySelector('dd')?.textContent ?? '').replace(/\s+/g, ' ').trim(),
    }));
  }

  it('lists the requirement a structure adds, the budget and the headroom left, then how many entries fit', async () => {
    await renderOptions(optionFacts(OPTION_REVIEW_WITH_BUDGET));

    expect(rows().map((row) => row.label)).toEqual([
      'Set aside for 2 structures', 'Premium', "Alpaca's requirement", 'Fees (estimate)',
      'Loss at expiry, if held, before fees (estimate)', 'Free in this budget after one entry',
      "What this adds to the account's requirement", 'Account headroom left after one entry',
    ]);
    expect(rows().at(-2)?.text).toBe('$0.00');
    expect(rows().at(-1)?.text).toBe('$4,175.91');
    expect(screen.getByText(/One whole entry fits right now under the bot's budget/)).toBeTruthy();
    expect(screen.getByText(/Every bot competes for the same account headroom/)).toBeTruthy();
  });

  it('says how many entries fit once when the review and the funding answer both carry it', async () => {
    const both = { ...optionFacts(OPTION_REVIEW_WITH_BUDGET), funding: COVERED };
    expect(both.option_review?.fits_sentence).toBe(COVERED.fits_sentence);

    await renderOptions(both);

    expect(occurrences(COVERED.fits_sentence ?? '')).toBe(1);
    // The funding card is still there with its limits and its own sentence.
    expect(limitRows().length).toBe(3);
    expect(occurrences(COVERED.sentence)).toBe(1);
  });

  it('says how many entries fit from the funding answer when the review carries no sentence for it', async () => {
    const review = { ...OPTION_REVIEW_WITH_BUDGET, fits: null, fits_sentence: null };

    await renderOptions({ ...optionFacts(review), funding: COVERED });

    expect(occurrences(COVERED.fits_sentence ?? '')).toBe(1);
  });

  it('shows the price each pick is assumed at beside the time the quotes were read', async () => {
    await renderOptions(optionFacts(OPTION_REVIEW_PRICED));

    expect(screen.getByText('at $4.12')).toBeTruthy();
    expect(screen.getByText(/priced at its ask and each sold one at its bid/)).toBeTruthy();
    expect(screen.getByText(/IBKR quotes read/)).toBeTruthy();
  });

  it('says what Alpaca’s netting would save and that the bot does not spend it', async () => {
    const netting = 'Alpaca values the account\'s positions together, so the account\'s requirement would fall by $500.00, '
      + 'though this structure requires $500.00 alone. The bot still sets aside $500.00: it does not spend the $1,000.00 that netting saves.';
    const review: DeploymentBudgetPreview['option_review'] = {
      ...OPTION_REVIEW_WITH_BUDGET,
      money_rows: (OPTION_REVIEW_WITH_BUDGET.money_rows ?? []).map((row) =>
        row.key === 'incremental_requirement' ? { ...row, amount_cents: -50_000, amount_usd: '-$500.00', note: netting } : row),
    };

    await renderOptions(optionFacts(review));

    const row = rows().find((candidate) => candidate.label === "What this adds to the account's requirement");
    expect(row?.text).toBe(`-$500.00${netting}`);
  });

  it('says a figure that is stale is stale with the time it was read, and one that is unknown is unknown and says why', async () => {
    const review: DeploymentBudgetPreview['option_review'] = {
      ...OPTION_REVIEW_WITH_BUDGET,
      money_rows: (OPTION_REVIEW_WITH_BUDGET.money_rows ?? []).map((row) => {
        if (row.key === 'headroom_left') return { ...row, state: 'stale' as const, as_of_ms: READ_AT - 60_000 };
        if (row.key === 'incremental_requirement') {
          return {
            ...row, amount_cents: null, amount_usd: null, state: 'unknown' as const,
            note: 'The structure sells more calls than it buys, so its loss grows without limit as the price rises.',
          };
        }
        return row;
      }),
    };

    await renderOptions(optionFacts(review));

    const stale = rows().find((row) => row.label === 'Account headroom left after one entry');
    const unknown = rows().find((row) => row.label === "What this adds to the account's requirement");
    expect(stale?.text).toMatch(/^\$4,175\.91 stale, as last read/);
    expect(unknown?.text).toBe(
      'unknownThe structure sells more calls than it buys, so its loss grows without limit as the price rises.',
    );
    expect(unknown?.text).not.toContain('$');
  });
});
