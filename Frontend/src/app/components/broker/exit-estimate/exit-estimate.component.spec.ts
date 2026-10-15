import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';
import type { ExitEstimateView, ExitLegQuoteView } from '../v2-panel/lib/broker-v2-panel.service';
import { ExitEstimateComponent } from './exit-estimate.component';

// 2026-10-16 16:00 America/New_York = 2026-10-16 20:00 UTC.
const OCT_16_2026_MS = Date.UTC(2026, 9, 16, 20, 0, 0);
const READ_AT = Date.UTC(2026, 9, 14, 15, 0, 0);

const SOLD = 'SPY261016P00505000';
const BOUGHT = 'SPY261016P00500000';

function leg(symbol: string, strike: string, contracts: number, changes: Partial<ExitLegQuoteView> = {}): ExitLegQuoteView {
  return {
    contract: { occ_symbol: symbol, underlying: 'SPY', expiry_ms: OCT_16_2026_MS, right: 'put', strike },
    contracts, state: 'known', age_ms: 1_000, missing: [], gap_usd: '0.10',
    ...changes,
  };
}

const FEES = 'Amounts are before fees. A positive amount is paid and a negative one comes back.';
const BUY_BACK_ALONE = 'Buying back a sold leg alone is a separate order from closing the whole structure, '
  + 'and Alpaca does not always fund it from the relief the close creates.';
const CLOSE = 'This system closes the position itself at Fri Oct 16, 15:50 ET: a sold option is not held overnight.';
const CUTOFF = "Alpaca's own expiry-day cutoff has not been verified.";

/** A $5-wide put spread deep in the money: it closes whole for $500.00 and needs $10,000.00 to buy its sold
 * leg back alone. Python worked every figure; the card shows them and compares nothing. */
const KNOWN: ExitEstimateView = {
  net_close: {
    state: 'known', amount_usd: '500.00', source: 'Quotes kept for this Dry Run', as_of_ms: READ_AT,
    terms: [
      { label: `Sell 1 ${BOUGHT} at the bid`, amount_usd: '-9500.00' },
      { label: `Buy back 1 ${SOLD} at the ask`, amount_usd: '10000.00' },
    ],
  },
  gross_buy_back: {
    state: 'known', amount_usd: '10000.00', source: 'Quotes kept for this Dry Run', as_of_ms: READ_AT,
    terms: [{ label: `Buy back 1 ${SOLD} at the ask`, amount_usd: '10000.00' }],
  },
  quotes: [leg(BOUGHT, '500', 1), leg(SOLD, '505', -1)],
  conditions: [FEES, BUY_BACK_ALONE, CLOSE, CUTOFF],
};

/** The bought leg's bid was received 81 seconds ago: past the age a held contract's mark is allowed. */
const STALE: ExitEstimateView = {
  ...KNOWN,
  net_close: { ...KNOWN.net_close, state: 'stale' },
  quotes: [leg(BOUGHT, '500', 1, { state: 'stale', age_ms: 81_000 }), leg(SOLD, '505', -1)],
};

/** A real account with no live quote: no amount for either figure, and the reason is the source. */
const UNKNOWN: ExitEstimateView = {
  net_close: {
    state: 'unknown', terms: [],
    source: "Live option quotes from this Clerk's IBKR feed, which cannot price this close: "
      + `${SOLD}: real accounts keep no option quotes, and this process has no live option quote source`,
  },
  gross_buy_back: {
    state: 'unknown', terms: [],
    source: "Live option quotes from this Clerk's IBKR feed, which cannot price this close: "
      + `${SOLD}: real accounts keep no option quotes, and this process has no live option quote source`,
  },
  quotes: [
    leg(BOUGHT, '500', 1, { state: 'unknown', age_ms: null, missing: ['bid', 'ask'], gap_usd: null }),
    leg(SOLD, '505', -1, { state: 'unknown', age_ms: null, missing: ['bid', 'ask'], gap_usd: null }),
  ],
  conditions: [FEES, BUY_BACK_ALONE, CUTOFF],
};

interface FigureRowText {
  readonly label: string;
  readonly amount: string | null;
  readonly state: string | null;
  readonly source: string | null;
  readonly terms: [string, string][];
}

function figureRows(): FigureRowText[] {
  const text = (row: Element, selector: string) => row.querySelector(selector)?.textContent?.trim() ?? null;
  return Array.from(document.querySelectorAll('.exit-estimate__figure')).map((row) => ({
    label: text(row, '.exit-estimate__label') ?? '',
    amount: text(row, '.exit-estimate__amount'),
    state: text(row, '.exit-estimate__state'),
    source: text(row, '.exit-estimate__source'),
    terms: Array.from(row.querySelectorAll('.exit-estimate__terms > div')).map((term): [string, string] => [
      term.querySelector('dt')?.textContent?.trim() ?? '',
      term.querySelector('dd')?.textContent?.trim() ?? '',
    ]),
  }));
}

/** Each leg's line as it reads: its contract, what is held, and what is said of its quote. */
function quoteRows(): string[] {
  return Array.from(document.querySelectorAll('.exit-estimate__quote'))
    .map((row) => Array.from(row.children).map((part) => part.textContent?.replace(/\s+/g, ' ').trim())
      .filter((part) => part !== undefined && part !== '')
      .join(' | '));
}

function conditionLines(): string[] {
  return Array.from(document.querySelectorAll('.exit-estimate__conditions > li')).map((line) => line.textContent?.trim() ?? '');
}

describe('What getting out costs', () => {
  it('shows both figures with their terms and source, each leg’s quote and the conditions, as Python wrote them', async () => {
    await render(ExitEstimateComponent, { inputs: { estimate: KNOWN } });

    expect(screen.getByRole('region', { name: 'What getting out costs' })).toBeTruthy();
    expect(figureRows()).toEqual([
      {
        label: 'Closing everything as one order', amount: '$500.00', state: null,
        source: 'Quotes kept for this Dry Run.',
        terms: [[`Sell 1 ${BOUGHT} at the bid`, '-$9,500.00'], [`Buy back 1 ${SOLD} at the ask`, '$10,000.00']],
      },
      {
        label: 'Buying back the sold legs alone', amount: '$10,000.00', state: null,
        source: 'Quotes kept for this Dry Run.',
        terms: [[`Buy back 1 ${SOLD} at the ask`, '$10,000.00']],
      },
    ]);
    expect(quoteRows()).toEqual([
      "SPY Put 500 Oct 16 '26 | bought 1 | 1 s old | gap $0.10 a share",
      "SPY Put 505 Oct 16 '26 | sold 1 | 1 s old | gap $0.10 a share",
    ]);
    expect(conditionLines()).toEqual([FEES, BUY_BACK_ALONE, CLOSE, CUTOFF]);
    expect(screen.queryByText(/stale/)).toBeNull();
    expect(screen.queryByText(/unknown/)).toBeNull();
  });

  it('says a stale figure is stale beside the amount it last had, and its leg is stale too', async () => {
    await render(ExitEstimateComponent, { inputs: { estimate: STALE } });

    expect(figureRows().map((row) => [row.label, row.amount, row.state])).toEqual([
      ['Closing everything as one order', '$500.00', 'stale, as last read'],
      ['Buying back the sold legs alone', '$10,000.00', null],
    ]);
    expect(quoteRows()[0]).toBe("SPY Put 500 Oct 16 '26 | bought 1 | 81 s old | gap $0.10 a share | stale");
    expect(quoteRows()[1]).not.toContain('stale');
  });

  it('says an unknown figure is unknown, with no amount and no zero, and gives the reason', async () => {
    await render(ExitEstimateComponent, { inputs: { estimate: UNKNOWN } });

    const rows = figureRows();
    expect(rows.map((row) => [row.label, row.amount, row.state, row.terms])).toEqual([
      ['Closing everything as one order', null, 'unknown', []],
      ['Buying back the sold legs alone', null, 'unknown', []],
    ]);
    expect(rows[0].source).toContain('real accounts keep no option quotes');
    expect(document.querySelector('.exit-estimate__figures')?.textContent).not.toContain('$');
    expect(quoteRows()).toEqual([
      "SPY Put 500 Oct 16 '26 | bought 1 | no bid or ask | unknown",
      "SPY Put 505 Oct 16 '26 | sold 1 | no bid or ask | unknown",
    ]);
    expect(conditionLines()).toEqual([FEES, BUY_BACK_ALONE, CUTOFF]);
  });

  it('says which side of a quote has not arrived, and shows no gap it cannot know', async () => {
    const estimate: ExitEstimateView = {
      ...KNOWN, quotes: [leg(BOUGHT, '500', 1, { missing: ['ask'], gap_usd: null }), leg(SOLD, '505', -1)],
    };
    await render(ExitEstimateComponent, { inputs: { estimate } });

    expect(quoteRows()[0]).toBe("SPY Put 500 Oct 16 '26 | bought 1 | 1 s old | no ask");
  });

  it('shows a buy-back of zero as a known amount when nothing is sold', async () => {
    const estimate: ExitEstimateView = {
      net_close: { ...KNOWN.net_close, amount_usd: '-9500.00', terms: [KNOWN.net_close.terms?.[0] ?? { label: '', amount_usd: '0' }] },
      gross_buy_back: { state: 'known', amount_usd: '0.00', source: 'No sold leg is held, so nothing is bought back', terms: [] },
      quotes: [leg(BOUGHT, '500', 1)],
      conditions: [FEES, CUTOFF],
    };
    await render(ExitEstimateComponent, { inputs: { estimate } });

    expect(figureRows().map((row) => [row.amount, row.state, row.source])).toEqual([
      ['-$9,500.00', null, 'Quotes kept for this Dry Run.'],
      ['$0.00', null, 'No sold leg is held, so nothing is bought back.'],
    ]);
    // The amount that rests on a quote says how old the oldest side of it is. The zero rests on none, and
    // says nothing of a quote's age.
    const [closed, bought] = Array.from(document.querySelectorAll('.exit-estimate__figure'));
    expect(closed.textContent).toContain('Oldest quote side received');
    expect(bought.textContent).not.toContain('Oldest quote side received');
  });
});
