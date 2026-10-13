import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';
import { fakeAccountPicture } from '../../../testing/account-money-fixtures';
import type { AccountPictureView, FigureView } from '../v2-panel/lib/broker-v2-panel.service';
import { AccountPictureComponent } from './account-picture.component';

// The option expiries are date-anchored: 16:00 America/New_York of their date.
const OCT_16_2026_MS = Date.UTC(2026, 9, 16, 20, 0, 0);
const OCT_23_2026_MS = Date.UTC(2026, 9, 23, 20, 0, 0);
const READ_AT = Date.UTC(2026, 9, 14, 15, 0, 0);

const EXPIRY_NOTE = "Each row is one underlying and expiry taken alone. Alpaca takes only the largest expiry of "
  + "each underlying, so the rows can add up to more than the account's collateral.";
const FEES = 'Amounts are before fees. A positive amount is paid and a negative one comes back.';
const BUY_BACK_ALONE = 'Buying back a sold leg alone is a separate order from closing the whole structure, '
  + 'and Alpaca does not always fund it from the relief the close creates.';

function figure(amount: string, terms: [string, string][], changes: Partial<FigureView> = {}): FigureView {
  return {
    state: 'known', amount_usd: amount, source: 'Clerk books', as_of_ms: READ_AT,
    terms: terms.map(([label, amount_usd]) => ({ label, amount_usd })),
    ...changes,
  };
}

/** $2,300.00 of cash. Bot a ($300.00 free) holds a $5-wide SPY put spread expiring Oct 16 and a $10-wide one
 * expiring Oct 23: Alpaca takes the larger expiry, $1,000.00. Stopped bot c holds a $3-wide QQQ spread,
 * $300.00. So $1,300.00 is collateral and $2,300.00 - $300.00 - $1,300.00 = $700.00 is unreserved. Each sold
 * leg can oblige its strike times 100 shares: SPY 505 and 510, QQQ 440. Python worked every figure; the card
 * shows them and adds none. */
const KNOWN: AccountPictureView = fakeAccountPicture({
  owned: {
    broker_cash: figure('2300.00', [['Cash as the account reports it', '2300.00']], { source: "Alpaca's account answer" }),
    marked_equity: figure('2310.00', [['Equity as the account reports it', '2310.00']], { source: "Alpaca's account answer" }),
    note: 'Marked equity is cash plus positions at their marks. Only cash can be spent; equity never is.',
  },
  spoken_for: {
    partition: figure('2300.00', [
      ['Set aside for running bots', '300.00'], ['Collateral held against sold options', '1300.00'],
      ['Held in entry orders', '0.00'], ['Outside purchases not in the cash yet', '0.00'], ['Fees not posted yet', '0.00'],
      ['Unreserved cash', '700.00'],
    ]),
    lines: [
      { kind: 'bot_free', label: 'Set aside for running bots', figure: figure('300.00', [['Bot a', '300.00']]) },
      {
        kind: 'collateral', label: 'Collateral held against sold options',
        figure: figure('1300.00', [['Bot a', '1000.00'], ['Stopped bot c', '300.00']]),
      },
      { kind: 'orders', label: 'Held in entry orders', figure: figure('0.00', []) },
      { kind: 'outside', label: 'Outside purchases not in the cash yet', figure: figure('0.00', []) },
      { kind: 'fees', label: 'Fees not posted yet', figure: figure('0.00', []) },
      {
        kind: 'unreserved', label: 'Unreserved cash',
        figure: figure('700.00', [
          ['Cash as the account reports it', '2300.00'], ['Set aside for running bots', '-300.00'],
          ['Collateral held against sold options', '-1300.00'],
        ]),
      },
    ],
    sentence: "These lines add up to the account's cash, to the cent.",
  },
  concentration: {
    account_collateral: figure('1300.00', [['QQQ', '300.00'], ['SPY', '1000.00']], {
      source: "Alpaca's published rule over every bot's contracts together, the largest expiry of each underlying",
    }),
    groups: [
      { underlying: 'QQQ', expiry_ms: OCT_16_2026_MS, collateral_usd: '300.00' },
      { underlying: 'SPY', expiry_ms: OCT_16_2026_MS, collateral_usd: '500.00' },
      { underlying: 'SPY', expiry_ms: OCT_23_2026_MS, collateral_usd: '1000.00' },
    ],
    expiry_note: EXPIRY_NOTE,
  },
  obligations: {
    assignment: figure('145500.00', [['QQQ', '44000.00'], ['SPY', '101500.00']]),
    assignment_note: 'The assignment obligation is the value, at the strikes, of the shares a sold option could '
      + 'oblige the account to buy or deliver. It is gross and is not a loss: a bounded loss is not a bounded '
      + 'obligation.',
    close_everything: figure('620.00', [['Bot a', '500.00'], ['Stopped bot c', '120.00']], {
      source: "The sum of the bots' exit estimates, each at the quotes held for its contracts",
    }),
    bots_holding: 2,
    bots_unpriced: 0,
    conditions: [FEES, BUY_BACK_ALONE],
  },
});

function rows(region: HTMLElement): { label: string; amount: string | null; state: string | null; terms: [string, string][] }[] {
  const text = (row: Element, selector: string) => row.querySelector(selector)?.textContent?.trim() ?? null;
  return Array.from(region.querySelectorAll('.figure-row')).map((row) => ({
    label: text(row, '.figure-row__label') ?? '',
    amount: text(row, '.figure-row__amount'),
    state: text(row, '.figure-row__state'),
    terms: Array.from(row.querySelectorAll('.figure-row__terms > div')).map((term): [string, string] => [
      term.querySelector('dt')?.textContent?.trim() ?? '',
      term.querySelector('dd')?.textContent?.trim() ?? '',
    ]),
  }));
}

describe('The account’s picture', () => {
  it('shows what the account owns as two figures, each with its source and when it was read', async () => {
    await render(AccountPictureComponent, { inputs: { picture: KNOWN } });

    const owned = screen.getByRole('region', { name: 'What the account owns' });
    expect(rows(owned).map((row) => [row.label, row.amount, row.state])).toEqual([
      ['Broker cash', '$2,300.00', null],
      ['Marked equity', '$2,310.00', null],
    ]);
    expect(within(owned).getAllByText("Alpaca's account answer.")).toHaveLength(2);
    expect(within(owned).getAllByText(/^As of/)).toHaveLength(2);
    expect(within(owned).getByText(KNOWN.owned.note)).toBeTruthy();
  });

  it('lists every line of what is spoken for, each opening into the bots it is made of by name', async () => {
    await render(AccountPictureComponent, { inputs: { picture: KNOWN } });

    const spoken = screen.getByRole('region', { name: 'What is spoken for' });
    expect(rows(spoken).map((row) => [row.label, row.amount])).toEqual([
      ['Set aside for running bots', '$300.00'],
      ['Collateral held against sold options', '$1,300.00'],
      ['Held in entry orders', '$0.00'],
      ['Outside purchases not in the cash yet', '$0.00'],
      ['Fees not posted yet', '$0.00'],
      ['Unreserved cash', '$700.00'],
    ]);
    const collateral = rows(spoken).find((row) => row.label === 'Collateral held against sold options');
    expect(collateral?.terms).toEqual([['Bot a', '$1,000.00'], ['Stopped bot c', '$300.00']]);
    // The cash the lines are parts of, and the backend's own sentence, once.
    expect(within(spoken).getByText('The account\'s cash').parentElement?.textContent).toContain('$2,300.00');
    expect(within(spoken).getAllByText("These lines add up to the account's cash, to the cent.")).toHaveLength(1);
  });

  it('shows the collateral by underlying and expiry beside the account’s own figure, and says why they differ once', async () => {
    await render(AccountPictureComponent, { inputs: { picture: KNOWN } });

    const concentration = screen.getByRole('region', { name: 'Where the collateral is' });
    expect(rows(concentration).map((row) => [row.label, row.amount, row.terms])).toEqual([
      ["The account's collateral, of the bots' contracts", '$1,300.00', [['QQQ', '$300.00'], ['SPY', '$1,000.00']]],
    ]);
    const groups = within(within(concentration).getByRole('list', { name: 'Collateral by underlying and expiry' }))
      .getAllByRole('listitem').map((item) => item.textContent?.replace(/\s+/g, ' ').trim());
    // An expiry is a date in the exchange's time, whatever zone the viewer is in.
    expect(groups).toEqual(["QQQ Oct 16 '26 $300.00", "SPY Oct 16 '26 $500.00", "SPY Oct 23 '26 $1,000.00"]);
    expect(within(concentration).getAllByText(EXPIRY_NOTE)).toHaveLength(1);
  });

  it('shows the assignment obligation apart from any loss, and the cost to close with its conditions', async () => {
    await render(AccountPictureComponent, { inputs: { picture: KNOWN } });

    const obligations = screen.getByRole('region', { name: 'What it could be obliged to, and what getting out costs' });
    expect(rows(obligations).map((row) => [row.label, row.amount, row.terms])).toEqual([
      ['Assignment obligation, gross', '$145,500.00', [['QQQ', '$44,000.00'], ['SPY', '$101,500.00']]],
      ['Cost to close every option position', '$620.00', [['Bot a', '$500.00'], ['Stopped bot c', '$120.00']]],
    ]);
    expect(within(obligations).getByText(/a bounded loss is not a bounded obligation/)).toBeTruthy();
    expect(within(obligations).getAllByRole('listitem').map((item) => item.textContent?.trim())).toEqual([FEES, BUY_BACK_ALONE]);
    expect(screen.getByText('These figures inform. Nothing here blocks an entry or sets a limit.')).toBeTruthy();
  });

  it('says a stale figure is stale beside the amount it last had', async () => {
    const stale: AccountPictureView = {
      ...KNOWN,
      owned: {
        ...KNOWN.owned,
        broker_cash: { ...KNOWN.owned.broker_cash, state: 'stale' },
      },
      spoken_for: {
        ...KNOWN.spoken_for,
        partition: { ...KNOWN.spoken_for.partition, state: 'stale' },
        lines: KNOWN.spoken_for.lines.map((line) => line.kind === 'unreserved'
          ? { ...line, figure: { ...line.figure, state: 'stale' as const } } : line),
      },
    };
    await render(AccountPictureComponent, { inputs: { picture: stale } });

    const states = Object.fromEntries(rows(document.body).map((row) => [row.label, row.state]));
    expect(states['Broker cash']).toBe('stale, as last read');
    expect(states['Unreserved cash']).toBe('stale, as last read');
    expect(states['Marked equity']).toBeNull();
    const total = screen.getByText("The account's cash").parentElement;
    expect(total?.textContent).toContain('$2,300.00');
    expect(total?.textContent).toContain('stale, as last read');
  });

  it('says an unknown figure is unknown, never zero, and states how many exit estimates are why', async () => {
    const reason = "1 of 2 bots' exit estimates cannot be priced, so the cost to close everything is not known: "
      + 'Stopped bot c: Quotes kept for this Dry Run, which cannot price this close: no quote has been kept for it yet';
    const unknown: AccountPictureView = {
      ...KNOWN,
      owned: {
        ...KNOWN.owned,
        marked_equity: { state: 'unknown', source: "Alpaca's account answer, which this reading did not carry", as_of_ms: READ_AT },
      },
      spoken_for: {
        partition: { state: 'unknown', source: "The account's cash cannot be partitioned: held in entry orders cannot be worked out" },
        lines: KNOWN.spoken_for.lines.map((line) => line.kind === 'orders'
          ? { ...line, figure: { state: 'unknown' as const, source: 'Clerk books and the account reading, which cannot split the account\'s order claims by bot' } }
          : line),
        sentence: "The lines cannot be added up to the account's cash: held in entry orders cannot be worked out.",
      },
      obligations: {
        ...KNOWN.obligations,
        close_everything: { state: 'unknown', source: reason },
        bots_unpriced: 1,
      },
    };
    await render(AccountPictureComponent, { inputs: { picture: unknown } });

    const owned = rows(screen.getByRole('region', { name: 'What the account owns' }));
    expect(owned[1]).toMatchObject({ label: 'Marked equity', amount: null, state: 'unknown' });
    const spoken = screen.getByRole('region', { name: 'What is spoken for' });
    expect(within(spoken).getByText("The lines cannot be added up to the account's cash: held in entry orders cannot be worked out."))
      .toBeTruthy();
    expect(rows(spoken).find((row) => row.label === 'Held in entry orders')).toMatchObject({ amount: null, state: 'unknown' });
    // The total of the cash the lines would be parts of says it is unknown, with no amount to read.
    expect(within(spoken).getByText("The account's cash").parentElement?.textContent).toContain('unknown');
    const close = rows(screen.getByRole('region', { name: 'What it could be obliged to, and what getting out costs' }))[1];
    expect(close).toMatchObject({ label: 'Cost to close every option position', amount: null, state: 'unknown', terms: [] });
    // The reason, with the count, is visible without opening anything.
    expect(screen.getByText(`${reason}.`)).toBeTruthy();
  });

  it('draws an account that holds no option as known zeros with no concentration rows', async () => {
    await render(AccountPictureComponent, { inputs: { picture: fakeAccountPicture() } });

    const concentration = screen.getByRole('region', { name: 'Where the collateral is' });
    expect(rows(concentration).map((row) => [row.label, row.amount])).toEqual([["The account's collateral, of the bots' contracts", '$0.00']]);
    expect(within(concentration).queryByRole('list')).toBeNull();
    const obligations = screen.getByRole('region', { name: 'What it could be obliged to, and what getting out costs' });
    expect(rows(obligations).map((row) => [row.label, row.amount])).toEqual([
      ['Assignment obligation, gross', '$0.00'], ['Cost to close every option position', '$0.00'],
    ]);
  });
});
