import { signal } from '@angular/core';
import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';
import { resourceTarget } from '../../../fleet/resource-target';
import {
  BrokerV2PanelService,
  type DeploymentBudgetView,
  type FundingView,
  type MoneySegment,
} from '../v2-panel/lib/broker-v2-panel.service';
import { type BotOpenPnl, DeploymentBudgetComponent } from './deployment-budget.component';

const TARGET = resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA9', bindingGeneration: 3, routingEpoch: 4 });

const RUNNING: DeploymentBudgetView = {
  state: 'ready', headline: 'Holding its position', detail: 'Its next entry waits until this position is sold.',
  strategy_instance_id: 'spy-ema-a', world: 'real_paper', entry_eligible: false,
  statement: [
    { label: 'Budget set aside at deploy', amount_usd: '1000.00' },
    { label: 'Realized gains and losses', amount_usd: '12.40' },
    { label: 'Fees', amount_usd: '-0.03' },
    { label: 'Balance', amount_usd: '1012.37', total: true },
    { label: 'In shares, at cost', amount_usd: '764.71' },
    { label: 'Waiting in entry orders', amount_usd: '0.00' },
    { label: 'Free to trade', amount_usd: '247.66' },
    { label: 'Short of its next entry', amount_usd: '0.00' },
  ],
  segment: {
    kind: 'bot', strategy_instance_id: 'spy-ema-a', label: 'spy-ema-a', amount_usd: '1012.37', share_bps: 10000, palette_index: 0,
    parts: { in_shares_usd: '764.71', in_shares_bps: 7554, pending_usd: '0.00', pending_bps: 0, free_usd: '247.66', free_bps: 2446 },
  },
  note: 'The budget limits new entries. Market fills and losses can go past it.',
  observed_at_ms: 1_800_000_000_000,
};

const READ_AT = 1_799_999_990_000;

/** The Clerk's answer for an $800.00 entry: $1,000.00 free, the account can fund $500.00, Alpaca's figure is
 * not read for this view. Python marks the account's headroom as the limit that binds. */
const FUNDING_REFUSED: FundingView = {
  free_budget: {
    state: 'known', amount_usd: '1000.00', source: 'Clerk books', as_of_ms: 1_800_000_000_000,
    terms: [{ label: 'Budget set aside at deploy', amount_usd: '1000.00' }],
  },
  account_headroom: {
    state: 'known', amount_usd: '500.00', source: 'Clerk books and the account reading', as_of_ms: READ_AT,
    terms: [
      { label: 'Unreserved account cash', amount_usd: '-500.00' },
      { label: "This bot's own claim", amount_usd: '1000.00' },
    ],
  },
  broker_buying_power: {
    state: 'unknown', terms: [],
    source: "Not read for this view. Alpaca's option buying power is read at Deploy and around each entry",
  },
  need: {
    commitment_usd: '800.00',
    terms: [{ label: 'Shares at the ask', amount_usd: '799.99' }, { label: 'Fee provision', amount_usd: '0.01' }],
  },
  admits: false, binding: 'account_headroom', shortfall_usd: '300.00', fits: 0,
  sentence: 'This bot has $1,000.00 free, but the account can fund only $500.00 after protecting other positions. '
    + 'The entry needs $800.00 including its fee provision. Shortfall: $300.00.',
  action: "Free another bot's budget, or add cash to the account.",
};

/** A $400.00 entry both limits cover: the budget has the least room, and nothing falls short of it. */
const FUNDING_PASSES: FundingView = {
  ...FUNDING_REFUSED,
  account_headroom: { ...FUNDING_REFUSED.account_headroom, amount_usd: '1000.00', terms: [
    { label: 'Unreserved account cash', amount_usd: '0.00' },
    { label: "This bot's own claim", amount_usd: '1000.00' },
  ] },
  need: { commitment_usd: '400.00', terms: [{ label: 'Shares at the ask', amount_usd: '399.99' }, { label: 'Fee provision', amount_usd: '0.01' }] },
  admits: true, binding: 'bot_budget', shortfall_usd: null, fits: 2, action: null,
  sentence: 'This bot has $1,000.00 free and the account can fund $1,000.00 after protecting other positions, '
    + "enough for the entry's $400.00 including its fee provision. "
    + 'Passing our own limits does not guarantee that Alpaca accepts the order.',
};

/** The same answer when the account's reading is past its age limit: the amount last worked out, said stale. */
const FUNDING_STALE: FundingView = {
  ...FUNDING_REFUSED,
  account_headroom: { ...FUNDING_REFUSED.account_headroom, state: 'stale' },
  admits: null, binding: 'bot_budget', shortfall_usd: null, fits: null, action: null,
  sentence: "This bot has $1,000.00 free. The account's headroom for it was last read more than 45 seconds ago, "
    + 'so it is not counted here. Whether the entry fits is not said. The entry needs $800.00 including its fee provision.',
};

/** Nothing could be worked out: no amount for any limit, never "$0.00". */
const FUNDING_UNKNOWN: FundingView = {
  free_budget: { state: 'unknown', source: 'Clerk books, which hold no budget for this bot', terms: [] },
  account_headroom: { state: 'unknown', source: 'Clerk books and the account reading, which could not be read for this bot', terms: [] },
  broker_buying_power: null, need: null, admits: null, binding: null, shortfall_usd: null, fits: null, action: null,
  sentence: "This bot's budget cannot be read right now, so what it can fund is unknown.",
};

/** An option bot's entry is priced when its contracts are picked: both limits are shown and nothing is judged. */
const FUNDING_UNPRICED: FundingView = {
  ...FUNDING_REFUSED,
  need: null, admits: null, binding: 'bot_budget', shortfall_usd: null, fits: null, action: null,
  account_headroom: { ...FUNDING_REFUSED.account_headroom, amount_usd: '1000.00', terms: [
    { label: 'Unreserved account cash', amount_usd: '0.00' },
    { label: "This bot's own claim", amount_usd: '1000.00' },
  ] },
  sentence: 'This bot has $1,000.00 free and the account can fund $1,000.00 after protecting other positions.',
};

/** What Python adds to a pass on the bot page, after the funding sentence. */
const STILL_CHECKED = ' Account risk is current. Order-time strategy, session and execution checks still apply.';

/** The bot page as Python serves a priced entry: its `detail` is the funding sentence itself, and a pass
 * follows the sentence with the note that order-time checks still apply. */
function pricedEntry(funding: FundingView, headline: string, detail: string): DeploymentBudgetView {
  return { ...RUNNING, headline, detail, funding };
}

const STOPPED_SEGMENT: MoneySegment = {
  kind: 'stopped', strategy_instance_id: 'spy-ema-b', label: 'held by stopped bot spy-ema-b', amount_usd: '670.43',
  share_bps: 10000, released_usd: '319.54', still_claimed_usd: '0.00', palette_index: 1,
};

/** Python's stopped statement: released, still in shares and still in entry
 * orders add up to its balance, and its slice is the one Home draws. */
const STOPPED: DeploymentBudgetView = {
  state: 'ready', headline: 'Stopped · still holds shares',
  detail: 'Its free budget was released when it stopped. The money in its shares comes back when they are sold.',
  strategy_instance_id: 'spy-ema-b', world: 'real_paper', entry_eligible: false,
  statement: [
    { label: 'Budget set aside at deploy', amount_usd: '1000.00' },
    { label: 'Realized gains and losses', amount_usd: '-10.00' },
    { label: 'Fees', amount_usd: '-0.03' },
    { label: 'Balance', amount_usd: '989.97', total: true },
    { label: 'Released at stop', amount_usd: '319.54' },
    { label: 'Still in shares, at cost', amount_usd: '670.43' },
    { label: 'Still in entry orders', amount_usd: '0.00' },
  ],
  segment: STOPPED_SEGMENT,
  note: null,
  observed_at_ms: 1_800_000_000_000,
};

/** A stopped bot whose shares cost more than its balance: Python names the overspend. */
const OVERSPENT: DeploymentBudgetView = {
  ...STOPPED,
  statement: [
    { label: 'Budget set aside at deploy', amount_usd: '1000.00' },
    { label: 'Realized gains and losses', amount_usd: '-400.00' },
    { label: 'Fees', amount_usd: '-0.03' },
    { label: 'Balance', amount_usd: '599.97', total: true },
    { label: 'Released at stop', amount_usd: '0.00' },
    { label: 'Still in shares, at cost', amount_usd: '670.43' },
    { label: 'Still in entry orders', amount_usd: '0.00' },
    { label: 'Over its budget by', amount_usd: '70.46' },
  ],
  segment: { ...STOPPED_SEGMENT, released_usd: '0.00' },
};

/** Stopped, flat and with nothing still claimed: it holds no money, so no slice. */
const FINISHED: DeploymentBudgetView = {
  ...STOPPED,
  headline: 'Stopped · fully released',
  statement: [
    { label: 'Budget set aside at deploy', amount_usd: '1000.00' },
    { label: 'Realized gains and losses', amount_usd: '-10.00' },
    { label: 'Fees', amount_usd: '-0.03' },
    { label: 'Balance', amount_usd: '989.97', total: true },
    { label: 'Released at stop', amount_usd: '989.97' },
    { label: 'Still in shares, at cost', amount_usd: '0.00' },
    { label: 'Still in entry orders', amount_usd: '0.00' },
  ],
  segment: null,
};

const UNAVAILABLE: DeploymentBudgetView = {
  state: 'unavailable', headline: "This bot's money is unavailable",
  detail: 'Wait for fresh account cash and risk evidence. The original commitment is retained.',
  strategy_instance_id: 'spy-ema-a', world: 'real_paper', committed_usd: '1000.00',
};

const LEGACY: DeploymentBudgetView = {
  state: 'legacy', headline: 'This bot has no budget',
  detail: 'This earlier deployment has no budget. Stop and reconcile it, then review a fresh Deploy.',
  strategy_instance_id: 'spy-ema-a', world: 'real_paper',
};

interface CardInputs {
  readonly holdsShares?: boolean;
  /** The panel's authored open gain or loss, as the page shell passes it. */
  readonly openPnl?: BotOpenPnl | null;
}

/** The panel's open P&L as Python authors it. */
function openPnl(usd: string | null, direction: BotOpenPnl['open_pnl_direction'] = null): BotOpenPnl {
  return { open_pnl_usd: usd, open_pnl_direction: direction };
}

/** Rendered where the bot page puts it — inside a landmark — with the page shell's bindings. */
async function renderCard(getBudget: ReturnType<typeof vi.fn>, inputs: CardInputs = {}) {
  const revision = signal(1);
  const result = await render(
    `<main aria-label="Bot"><app-deployment-budget [target]="target" strategyInstanceId="spy-ema-a" [revision]="revision()"
      [holdsShares]="holdsShares" [openPnl]="openPnl" /></main>`,
    {
      imports: [DeploymentBudgetComponent],
      componentProperties: {
        target: TARGET, revision,
        holdsShares: inputs.holdsShares ?? false, openPnl: inputs.openPnl ?? null,
      },
      providers: [{ provide: BrokerV2PanelService, useValue: { getBudget } }],
    },
  );
  return { ...result, revision };
}

function statementRows(): [string, string, boolean][] {
  const card = screen.getByRole('region', { name: "This bot's money" });
  const list = card.querySelector('dl');
  if (list === null) throw new Error('the statement is not rendered');
  return Array.from(list.querySelectorAll('.deployment-budget__line')).map((row) => [
    row.querySelector('dt')?.textContent?.trim() ?? '',
    row.querySelector('dd')?.textContent?.trim() ?? '',
    row.classList.contains('deployment-budget__line--total'),
  ]);
}

function slices(container: Element): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>('.money-bar__track > .money-bar__slice'));
}

function parts(container: Element): [string, string][] {
  return Array.from(container.querySelectorAll<HTMLElement>('.money-bar__part'))
    .map((part) => [part.className, part.style.flexGrow]);
}

interface LimitRowText {
  readonly label: string;
  readonly amount: string | null;
  readonly state: string | null;
  readonly mark: string | null;
  readonly terms: [string, string][];
}

/** Each limit the funding block lists, as it reads on the page and with the terms it opens to. */
function limitRows(): LimitRowText[] {
  const text = (row: Element, selector: string) => row.querySelector(selector)?.textContent?.trim() ?? null;
  return Array.from(document.querySelectorAll('.funding-answer__limit')).map((row) => ({
    label: text(row, '.funding-answer__label') ?? '',
    amount: text(row, '.funding-answer__amount'),
    state: text(row, '.funding-answer__state'),
    mark: text(row, '.funding-answer__mark'),
    terms: Array.from(row.querySelectorAll('.funding-answer__terms > div')).map((term): [string, string] => [
      term.querySelector('dt')?.textContent?.trim() ?? '',
      term.querySelector('dd')?.textContent?.trim() ?? '',
    ]),
  }));
}

/** How many times `text` is on the page. */
function occurrences(text: string): number {
  return (document.body.textContent ?? '').split(text).length - 1;
}

async function expectAxeClean(): Promise<void> {
  const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
  expect(results.violations).toEqual([]);
}

describe("This bot's money", () => {
  it('shows a running bot’s statement verbatim, in order, with Balance as its total', async () => {
    const { container } = await renderCard(vi.fn().mockResolvedValue(RUNNING));

    await screen.findByText('Holding its position');
    expect(screen.getByText('Its next entry waits until this position is sold.')).toBeTruthy();
    expect(statementRows()).toEqual([
      ['Budget set aside at deploy', '$1,000.00', false],
      ['Realized gains and losses', '$12.40', false],
      ['Fees', '-$0.03', false],
      ['Balance', '$1,012.37', true],
      ['In shares, at cost', '$764.71', false],
      ['Waiting in entry orders', '$0.00', false],
      ['Free to trade', '$247.66', false],
      ['Short of its next entry', '$0.00', false],
    ]);
    expect(screen.getByText('The budget limits new entries. Market fills and losses can go past it.')).toBeTruthy();
    expect(container.querySelector('app-timestamp-display')).not.toBeNull();
    expect(screen.queryByText(/Budget (and|or) account risk/)).toBeNull();
    expect(screen.queryByText(/Stopped does not mean flat/)).toBeNull();
    expect(screen.queryByText('Paper')).toBeNull();
  });

  it('draws the running bot’s slice exactly as Python wrote it — Home’s slice, shaded by its parts', async () => {
    const { container } = await renderCard(vi.fn().mockResolvedValue(RUNNING));
    await screen.findByText('Holding its position');

    const drawn = slices(container);
    expect(drawn.map((slice) => [slice.className, slice.style.flexGrow])).toEqual([
      ['money-bar__slice money-bar__slice--bot', '10000'],
    ]);
    expect(parts(container)).toEqual([
      ['money-bar__part money-bar__part--shares', '7554'],
      ['money-bar__part money-bar__part--pending', '0'],
      ['money-bar__part money-bar__part--free', '2446'],
    ]);
    const legend = screen.getByRole('list', { name: "This bot's slice" });
    expect(within(legend).getByRole('listitem').textContent?.replace(/\s+/g, ' ').trim())
      .toBe('spy-ema-a $1,012.37 in shares $764.71 · in entry orders $0.00 · free $247.66');
  });

  it('shows a stopped bot’s released and still-held money adding up to its balance, with Home’s striped slice', async () => {
    const { container } = await renderCard(vi.fn().mockResolvedValue(STOPPED));

    await screen.findByText('Stopped · still holds shares');
    expect(statementRows()).toEqual([
      ['Budget set aside at deploy', '$1,000.00', false],
      ['Realized gains and losses', '-$10.00', false],
      ['Fees', '-$0.03', false],
      ['Balance', '$989.97', true],
      ['Released at stop', '$319.54', false],
      ['Still in shares, at cost', '$670.43', false],
      ['Still in entry orders', '$0.00', false],
    ]);
    expect(slices(container).map((slice) => [slice.className, slice.style.flexGrow])).toEqual([
      ['money-bar__slice money-bar__slice--stopped', '10000'],
    ]);
    expect(parts(container)).toEqual([]);
    const legend = screen.getByRole('list', { name: "This bot's slice" });
    expect(within(legend).getByRole('listitem').textContent?.replace(/\s+/g, ' ').trim())
      .toBe('held by stopped bot spy-ema-b $670.43 released $319.54 · still claimed $0.00');
  });

  it('names what a stopped bot spent beyond its balance, only when Python says it did', async () => {
    await renderCard(vi.fn().mockResolvedValue(OVERSPENT));

    await screen.findByText('Stopped · still holds shares');
    expect(statementRows().slice(3)).toEqual([
      ['Balance', '$599.97', true],
      ['Released at stop', '$0.00', false],
      ['Still in shares, at cost', '$670.43', false],
      ['Still in entry orders', '$0.00', false],
      ['Over its budget by', '$70.46', false],
    ]);
  });

  it('shows what the Clerk answered: the sentence once, what to do, the shortfall and every limit with the binding one marked', async () => {
    await renderCard(vi.fn().mockResolvedValue(pricedEntry(FUNDING_REFUSED, 'Its next entry waits', FUNDING_REFUSED.sentence)));

    await screen.findByText('Its next entry waits');
    expect(occurrences(FUNDING_REFUSED.sentence)).toBe(1);
    expect(screen.getByText(/Free another bot's budget, or add cash to the account\./)).toBeTruthy();
    expect(screen.getByText(/Short of this entry by/).textContent?.replace(/\s+/g, ' ').trim()).toBe('Short of this entry by $300.00');
    expect(limitRows()).toEqual([
      {
        label: 'Free budget', amount: '$1,000.00', state: null, mark: null,
        terms: [['Budget set aside at deploy', '$1,000.00']],
      },
      {
        label: 'Account headroom', amount: '$500.00', state: null, mark: 'binds',
        terms: [['Unreserved account cash', '-$500.00'], ["This bot's own claim", '$1,000.00']],
      },
      { label: 'Alpaca option buying power', amount: null, state: 'unknown', mark: null, terms: [] },
    ]);
    expect(screen.getByText(/read at Deploy and around each entry/)).toBeTruthy();
  });

  it('marks the limit with the least room as such, not as binding, when the entry passes, and says the sentence once', async () => {
    const view = pricedEntry(FUNDING_PASSES, 'Ready for its next entry', FUNDING_PASSES.sentence + STILL_CHECKED);
    await renderCard(vi.fn().mockResolvedValue(view));

    await screen.findByText('Ready for its next entry');
    expect(occurrences(FUNDING_PASSES.sentence)).toBe(1);
    expect(screen.getByText(/Order-time strategy, session and execution checks still apply\./)).toBeTruthy();
    expect(limitRows().map((row) => [row.label, row.mark])).toEqual([
      ['Free budget', 'least room'],
      ['Account headroom', null],
      ['Alpaca option buying power', null],
    ]);
    expect(screen.queryByText('binds')).toBeNull();
    expect(screen.queryByText(/Short of this entry/)).toBeNull();
    expect(screen.queryByText(/What you can do/)).toBeNull();
  });

  it('says a stale figure is stale beside the amount it last had, and promises nothing', async () => {
    await renderCard(vi.fn().mockResolvedValue(pricedEntry(FUNDING_STALE, 'Its next entry waits', FUNDING_STALE.sentence)));

    await screen.findByText('Its next entry waits');
    expect(occurrences(FUNDING_STALE.sentence)).toBe(1);
    expect(limitRows().map((row) => [row.label, row.amount, row.state, row.mark])).toEqual([
      ['Free budget', '$1,000.00', null, 'least room'],
      ['Account headroom', '$500.00', 'stale, as last read', null],
      ['Alpaca option buying power', null, 'unknown', null],
    ]);
    expect(screen.queryByText(/Short of this entry/)).toBeNull();
    expect(screen.queryByText(/What you can do/)).toBeNull();
  });

  it('says an unknown figure is unknown, with no amount and no zero, and omits a broker row the bot has none of', async () => {
    await renderCard(vi.fn().mockResolvedValue(pricedEntry(FUNDING_UNKNOWN, 'Its next entry waits', FUNDING_UNKNOWN.sentence)));

    await screen.findByText('Its next entry waits');
    expect(occurrences(FUNDING_UNKNOWN.sentence)).toBe(1);
    expect(limitRows()).toEqual([
      { label: 'Free budget', amount: null, state: 'unknown', mark: null, terms: [] },
      { label: 'Account headroom', amount: null, state: 'unknown', mark: null, terms: [] },
    ]);
    expect(document.querySelector('.funding-answer')?.textContent).not.toContain('$');
  });

  it('keeps an unpriced option entry’s own detail and still lists both limits', async () => {
    const detail = 'Its next entry is in option contracts, and their cost is known only when they are picked at the entry.';
    await renderCard(vi.fn().mockResolvedValue(
      pricedEntry(FUNDING_UNPRICED, 'Its next entry is priced when its contracts are picked', detail),
    ));

    await screen.findByText(detail);
    expect(occurrences(detail)).toBe(1);
    expect(limitRows().map((row) => [row.label, row.amount, row.mark])).toEqual([
      ['Free budget', '$1,000.00', 'least room'],
      ['Account headroom', '$1,000.00', null],
      ['Alpaca option buying power', null, null],
    ]);
    expect(screen.queryByText(/Short of this entry/)).toBeNull();
  });

  it('shows no funding block for a bot whose view carries none', async () => {
    const { container } = await renderCard(vi.fn().mockResolvedValue(STOPPED));

    await screen.findByText('Stopped · still holds shares');
    expect(container.querySelector('app-funding-answer')).toBeNull();
  });

  it('draws no slice for a finished bot, which holds no money', async () => {
    const { container } = await renderCard(vi.fn().mockResolvedValue(FINISHED));

    await screen.findByText('Stopped · fully released');
    expect(container.querySelector('app-money-bar')).toBeNull();
    expect(statementRows()).toContainEqual(['Released at stop', '$989.97', false]);
  });

  it.each([['unavailable', UNAVAILABLE], ['legacy', LEGACY]])('shows only the headline and detail when the money is %s', async (_state, view) => {
    const { container } = await renderCard(vi.fn().mockResolvedValue(view), { holdsShares: true, openPnl: openPnl('4.50', 'gain') });

    await screen.findByText(view.headline);
    expect(screen.getByText(view.detail)).toBeTruthy();
    expect(container.querySelector('app-money-bar')).toBeNull();
    expect(container.querySelector('dl')).toBeNull();
    expect(screen.queryByText('Unknown')).toBeNull();
    expect(screen.queryByText(/Open gain or loss/)).toBeNull();
    expect(screen.queryByText(/Budget (and|or) account risk/)).toBeNull();
  });

  it('notes the open gain or loss on shares apart from the bar, in Python’s own dollars', async () => {
    await renderCard(vi.fn().mockResolvedValue(RUNNING), { holdsShares: true, openPnl: openPnl('-3.25', 'loss') });

    const note = await screen.findByText(/Open gain or loss on shares:/);
    expect(note.textContent?.replace(/\s+/g, ' ').trim()).toBe('Open gain or loss on shares: -$3.25, counted when sold.');
    expect(note.querySelector('.num.negative')?.textContent).toBe('-$3.25');
  });

  it.each([
    ['gain', '12.40', '$12.40', 'positive'],
    ['loss', '-0.01', '-$0.01', 'negative'],
    ['flat', '0.00', '$0.00', null],
  ] as const)('colours the open gain or loss by Python’s direction (%s), never by reading the number', async (direction, usd, shown, tone) => {
    await renderCard(vi.fn().mockResolvedValue(RUNNING), { holdsShares: true, openPnl: openPnl(usd, direction) });

    const note = await screen.findByText(/Open gain or loss on shares:/);
    const amount = note.querySelector<HTMLElement>('.num');
    expect(amount?.textContent).toBe(shown);
    expect([amount?.classList.contains('positive'), amount?.classList.contains('negative')])
      .toEqual([tone === 'positive', tone === 'negative']);
  });

  it('has no open gain or loss note when it holds no shares', async () => {
    await renderCard(vi.fn().mockResolvedValue(RUNNING), { holdsShares: false, openPnl: openPnl('7.00', 'gain') });

    await screen.findByText('Holding its position');
    expect(screen.queryByText(/Open gain or loss/)).toBeNull();
  });

  it('reads the money again for a new revision', async () => {
    const getBudget = vi.fn().mockResolvedValue(RUNNING);
    const { fixture, revision } = await renderCard(getBudget);
    await screen.findByText('Holding its position');

    revision.set(2);
    await fixture.whenStable();

    expect(getBudget).toHaveBeenCalledTimes(2);
    expect(getBudget).toHaveBeenLastCalledWith(TARGET, 'spy-ema-a');
  });

  it('says so when the money cannot be read, and reads again on Refresh', async () => {
    const getBudget = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(RUNNING);
    await renderCard(getBudget);

    expect((await screen.findByRole('alert')).textContent).toBe("This bot's money could not be read.");
    await userEvent.click(screen.getByRole('button', { name: "Refresh this bot's money" }));

    expect(await screen.findByText('Holding its position')).toBeTruthy();
    expect(getBudget).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: "Refresh this bot's money" })).toBeTruthy();
  });

  it.each([
    ['running', RUNNING],
    ['stopped', STOPPED],
    ['running with its funding answer', { ...RUNNING, funding: FUNDING_REFUSED }],
  ])('has no detectable accessibility violations when %s', async (_name, view) => {
    await renderCard(vi.fn().mockResolvedValue(view), { holdsShares: true, openPnl: openPnl('12.50', 'gain') });
    await screen.findByText(view.headline);

    await expectAxeClean();
  });
});
