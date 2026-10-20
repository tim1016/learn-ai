import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { expectTradingDatesInNewYork, reads } from '../testing/page-actions';
import type { RunDetail, RunFigures } from '../options-backtest.types';
import { figures, runBanner, runDetail, runWithoutSetup } from '../testing/run-fixtures';
import { RunDetailService } from '../run-detail/run-detail.service';
import { inertRunDetail } from '../run-detail/testing/fake-run-detail';
import { RunResultComponent } from './run-result.component';

/** The closer look at the trades is not what these specs are about, so it reads nothing. */
const providers = [{ provide: RunDetailService, useValue: inertRunDetail() }];

async function renderResult(changes: Partial<RunDetail> = {}, figureChanges: Partial<RunFigures> = {}) {
  const run = runDetail({ ...changes, figures: figures(figureChanges) });
  return render(RunResultComponent, { inputs: { run }, providers });
}

const tile = (name: RegExp | string) => {
  const label = screen.getByText(name, { selector: 'dt, dt > button' });
  return label.closest('.tile') as HTMLElement;
};

describe('the banner', () => {
  it('says the fills are simulated from trade prices, bid and ask are not available, the slippage and price step assumed, and the fees in the server’s words', async () => {
    await renderResult({ banner: runBanner({ fee_policy: 'Fees were set by the owner at 0.65 a contract.', slippage_percent: '0.45', price_step_usd: '0.05' }) });

    const banner = screen.getByRole('note', { name: 'How these figures were made' });

    expect([...banner.querySelectorAll('p')].map((line) => reads(line))).toEqual([
      'Fills are simulated from trade prices.',
      'Bid and ask are not available.',
      'Slippage assumed: 0.45%. Price step: $0.05.',
      'Fees: Fees were set by the owner at 0.65 a contract.',
    ]);
  });

  it('is above the figures, always', async () => {
    const { container } = await renderResult();

    const banner = container.querySelector('app-options-backtest-run-banner');
    const headline = container.querySelector('app-options-backtest-run-headline');

    expect(banner?.compareDocumentPosition(headline as Node)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });

  it('says what the server says, whichever way the flags point', async () => {
    await renderResult({ banner: runBanner({ fills_are_simulated_from_trade_prices: false, bid_and_ask_are_available: true }) });

    expect(screen.getByText('Fills are not simulated from trade prices.')).toBeTruthy();
    expect(screen.getByText('Bid and ask are available.')).toBeTruthy();
  });
});

describe('the headline figures', () => {
  it('shows net profit, ending cash, fees and slippage paid exactly as the server wrote them, the trade count, and what each win rate counts', async () => {
    await renderResult();

    expect.soft(reads(tile('Net profit')), 'shows net profit exactly as the server wrote it').toBe('Net profit $1,234.56 After modeled costs');
    expect.soft(reads(tile('Ending cash')), 'shows ending cash exactly as the server wrote it').toBe('Ending cash $11,234.56');
    expect.soft(reads(tile('Total fees')), 'shows fees exactly as the server wrote them').toBe('Total fees $46.80');
    expect.soft(reads(tile('Slippage paid')), 'shows slippage paid exactly as the server wrote it').toBe('Slippage paid $88.12');
    expect.soft(reads(tile('Trades')), 'shows the trade count with its won, lost and break-even trades').toBe('Trades 12 won 7 · lost 4 · break-even 1');
    expect.soft(reads(tile(/^Trade win rate/)), 'names what the trade win rate counts, shows the server’s rounded figure marked as rounded, and the two whole numbers beside it').toBe('Trade win rate 58.3% · 0.583333 rounded to 6 places · 7 of 12');
    expect.soft(reads(tile(/^Daily win rate/)), 'names what the daily win rate counts, shows the server’s rounded figure marked as rounded, and the two whole numbers beside it').toBe('Daily win rate 62.5% · 0.625000 rounded to 6 places · 5 of 8');
  });

  it('passes a dollar string through without turning it into a number: the digits a float cannot hold survive', async () => {
    // 9007199254740993 is the first whole number a double cannot hold; a Number() on it would show ...992.
    await renderResult({}, { net_profit_usd: '9007199254740993.01', ending_cash_usd: '-0.01', total_fees_usd: '1234567890123456.70', slippage_paid_usd: '0.10' });

    expect(reads(tile('Net profit'))).toBe('Net profit $9,007,199,254,740,993.01 After modeled costs');
    expect(reads(tile('Ending cash'))).toBe('Ending cash -$0.01');
    expect(reads(tile('Total fees'))).toBe('Total fees $1,234,567,890,123,456.70');
    expect(reads(tile('Slippage paid'))).toBe('Slippage paid $0.10');
  });

  it('shows a string that is not a two-place amount as it came, rather than a rounded one', async () => {
    await renderResult({}, { net_profit_usd: '123.4567' });

    expect(reads(tile('Net profit'))).toBe('Net profit 123.4567 After modeled costs');
  });

  it('shows the rounded rate the server gave, and works out none: an inconsistent figure is shown as it came', async () => {
    await renderResult({}, { trade_win_rate: { numerator: '7', denominator: '12', rounded_to_6_places: '0.999999', display_percent_1_place: '100.0' } });

    expect(reads(tile(/^Trade win rate/))).toContain('0.999999');
    expect(reads(tile(/^Trade win rate/))).not.toContain('0.583333');
  });

  it('says there were no trades, and no traded sessions, when a rate has nothing to count', async () => {
    await renderResult({}, { trade_win_rate: null, daily_win_rate: null });

    expect(reads(tile(/^Trade win rate/))).toContain('No trades');
    expect(reads(tile(/^Daily win rate/))).toContain('No traded sessions');
  });

  it('shows the profit factor with the two sums it is of, or says there was no losing trade', async () => {
    const { rerender } = await render(RunResultComponent, { inputs: { run: runDetail() }, providers });

    expect(reads(tile(/^Profit factor/))).toBe('Profit factor 2.612876 rounded to 6 places · $2,000.00 over $765.44');

    await rerender({ inputs: { run: runDetail({ figures: figures({ profit_factor: null }) }) } });

    expect(reads(tile(/^Profit factor/))).toContain('No losing trade');
  });
});

describe('the largest falls', () => {
  it('shows both, each with its peak and trough and their dates in New York dates, and says they are measured at session ends', async () => {
    await renderResult();

    const section = screen.getByRole('region', { name: 'Maximum drawdown' });
    expect.soft(within(section).getByText('Both are measured at the end of each session, not minute by minute.'), 'says they are measured at session ends').toBeTruthy();
    const [dollars, fraction] = [...section.querySelectorAll('.fall')];
    expect.soft(reads(dollars), 'shows the largest fall in dollars with its peak and trough and their dates').toBe(
      'Largest fall in dollars $412.30 As a fraction of the peak: 0.037826 (rounded to 6 places) Peak equity: $10,900.00 at the end of 2026-09-15 Trough equity: $10,487.70 at the end of 2026-09-17',
    );
    expect.soft(reads(fraction), 'shows the largest fall as a fraction with its peak and trough and their dates').toBe(
      'Largest fall as a fraction of the peak $400.00 As a fraction of the peak: 0.040000 (rounded to 6 places) Peak equity: $10,000.00 before the first session Trough equity: $9,600.00 at the end of 2026-09-14',
    );
    expectTradingDatesInNewYork(section);
  });

  it('says equity never fell when the server has no fall to report', async () => {
    await renderResult({}, { drawdown_by_dollars: null, drawdown_by_fraction: null });

    expect(screen.getAllByText('Equity never fell below a peak at the end of a session.')).toHaveLength(2);
  });
});

describe('the sessions and what did not trade', () => {
  it('counts the sessions as the server does, and the skipped ones by reason, zeros included, and counts what did not trade and the picks refused by reason', async () => {
    await renderResult();

    const section = screen.getByRole('region', { name: 'Sessions' });
    const count = (label: string) => (within(section).getByText(label, { selector: 'dt' }).nextElementSibling as HTMLElement).textContent;
    expect.soft(['Tested', 'Traded', 'No trade', 'Skipped', 'Incomplete', 'Profitable', 'Losing', 'Break-even'].map(count), 'counts the sessions as the server does').toEqual(['10', '8', '1', '1', '1', '5', '2', '1']);
    expect.soft(within(section).getByText('Window Does Not Fit: 1'), 'counts the skipped sessions by reason: window does not fit').toBeTruthy();
    expect.soft(within(section).getByText('Underlying Did Not Trade: 0'), 'counts the skipped sessions by reason, zeros included: underlying did not trade').toBeTruthy();

    const notTraded = screen.getByRole('region', { name: 'What did not trade' });
    const notTradedCount = (label: string) => (within(notTraded).getByText(label, { selector: 'dt' }).nextElementSibling as HTMLElement).textContent;
    expect.soft(
      ['Sessions without momentum', 'Entries refused for cash', 'Re-entries blocked by the cutoff', 'Entries dropped', 'Legs without a starting price', 'Re-entries lost'].map(notTradedCount),
      'counts what did not trade, each named for what it counts',
    ).toEqual(['3', '2', '1', '4', '5', '6']);
    expect.soft(within(notTraded).getByText('Chain Incomplete: 1'), 'counts the picks refused by reason: chain incomplete').toBeTruthy();
    expect.soft(within(notTraded).getByText('Strike Not Listed: 2'), 'counts the picks refused by reason: strike not listed').toBeTruthy();
    expect.soft(within(notTraded).getByText('Also counted under the causes above; not to be added to them.'), 'says the picks are also counted under the causes above').toBeTruthy();
  });

  const refusals = (section: HTMLElement) =>
    [
      'Entries refused: their cover did not trade',
      'Entries refused: their cover was not yet held',
      'Entries refused for a second expiry',
      'Entries refused for a contract held on the other side',
      'Sales to open not taken at zero',
    ].map((label) => (within(section).getByText(label, { selector: 'dt' }).nextElementSibling as HTMLElement).textContent?.trim());

  it('counts each refusal of a sale and of its cover under its own name, the cover that did not trade apart from the cover not yet held (#3109)', async () => {
    await renderResult(
      {},
      {
        entries_refused_by_reason: [
          { reason: 'other_side', count: 4 },
          { reason: 'second_expiry', count: 3 },
          { reason: 'cover_did_not_trade', count: 1 },
          { reason: 'cover_not_yet_held', count: 2 },
          { reason: 'sale_at_zero', count: 5 },
        ],
      },
    );

    const section = screen.getByRole('region', { name: 'What did not trade' });

    expect(refusals(section)).toEqual(['1', '2', '3', '4', '5']);
    // The count for cash is its own, as it was.
    expect((within(section).getByText('Entries refused for cash', { selector: 'dt' }).nextElementSibling as HTMLElement).textContent).toBe('2');
  });

  it('reads each of them as not recorded on a run saved before they were counted, and not as none', async () => {
    await renderResult({}, { entries_refused_by_reason: undefined });

    const section = screen.getByRole('region', { name: 'What did not trade' });

    expect(refusals(section)).toEqual(['not recorded', 'not recorded', 'not recorded', 'not recorded', 'not recorded']);
  });

  it('shows a reason this page has no words for as a receipt label, never as its raw token', async () => {
    await renderResult({}, { entries_refused_by_reason: [{ reason: 'cash_short' as never, count: 7 }] });

    const section = screen.getByRole('region', { name: 'What did not trade' });

    expect(within(section).getByText('Cash Short', { selector: 'dt' }).nextElementSibling?.textContent?.trim()).toBe('7');
    expect(section.textContent).not.toContain('cash_short');
  });
});

describe('the three statements of a result that sells (#3109)', () => {
  const STATEMENTS = [
    'Legs opened in the same minute are treated as one group, and minute bars cannot show which of them printed first. A sold leg’s cover is judged as if it were held beside it, which is a modelling choice and not observed coverage.',
    'The requirement held against what is sold is Alpaca’s published rule for covered positions as of today, worked out by this system.',
    'This result claims nothing about whether a broker would have accepted these orders.',
  ];

  it('shows each as the server wrote it, after the lines that say how the fills were made', async () => {
    await renderResult({ banner: runBanner({ statements: STATEMENTS }) });

    const banner = screen.getByRole('note', { name: 'How these figures were made' });

    expect([...banner.querySelectorAll('p')].map((line) => reads(line)).slice(4)).toEqual(STATEMENTS);
  });

  it('shows none on a result that only buys', async () => {
    await renderResult({ banner: runBanner({ statements: [] }) });

    const banner = screen.getByRole('note', { name: 'How these figures were made' });

    expect(banner.querySelectorAll('p')).toHaveLength(4);
  });
});

describe('the slippage tolerance', () => {
  const sentence = () => screen.getByRole('region', { name: 'Slippage tolerance' }).querySelector('.sentence')?.textContent;

  it('says how far the result survives and where it first loses, and shows the ladder with the setup’s own row marked', async () => {
    await renderResult();

    expect.soft(sentence(), 'says how far the result survives and where it first loses').toBe('The result survives up to 0.3% slippage. It is first unprofitable at 0.6%.');
    const rows = [...screen.getByRole('table', { name: 'The run replayed at each slippage' }).querySelectorAll('tbody tr')].map((row) => reads(row));
    expect.soft(rows, 'shows the ladder, each figure as the server wrote it').toEqual(['0.0 $1,500.00 12 8 6', '0.3 this setup $1,234.56 12 8 5', '0.6 -$12.34 11 8 3']);
    const own = screen.getByText('this setup').closest('tr');
    expect.soft(own?.classList.contains('own'), 'marks the setup’s own row').toBe(true);
  });

  it('says it survives the whole ladder', async () => {
    await renderResult({ slippage_tolerance: { highest_profitable_percent: '0.6', first_unprofitable_percent: null, nothing_traded: false } });

    expect(sentence()).toBe('The result survives the whole ladder, up to 0.6% slippage.');
  });

  it('says nothing traded, which is not a loss at every rung', async () => {
    await renderResult({ slippage_tolerance: { highest_profitable_percent: null, first_unprofitable_percent: null, nothing_traded: true } });

    expect(sentence()).toBe('Nothing traded, so there is no slippage tolerance to read.');
  });
});

describe('a complete run with no figures', () => {
  it('says so rather than showing an empty result', async () => {
    await render(RunResultComponent, { inputs: { run: runDetail({ figures: null }) }, providers });

    expect(screen.getByRole('alert').textContent).toBe('This run is complete but the server sent no figures for it.');
  });
});

describe('a complete run whose setup and banner the server could not open', () => {
  it('still shows its figures, with no banner, rather than failing to draw', async () => {
    const { container } = await render(RunResultComponent, { inputs: { run: runWithoutSetup() }, providers });

    expect(screen.queryByRole('note', { name: 'How these figures were made' })).toBeNull();
    expect(container.querySelector('app-options-backtest-run-banner')).toBeNull();
    expect(reads(tile('Net profit'))).toBe('Net profit $1,234.56 After modeled costs');
    expect(screen.getByRole('region', { name: 'Sessions' })).toBeTruthy();
    expect(screen.getByRole('region', { name: 'By month' })).toBeTruthy();
  });
});
