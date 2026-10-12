import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { sessionOpenMs } from '../../../shared/date-picker/testing/fake-trading-calendar';
import type { DayRow, Drawdown, MonthRow, RunDetail } from '../options-backtest.types';
import { reads } from '../testing/page-actions';
import { DAY_ROWS, figures, LADDER, MONTH_ROWS, runBanner, runDetail } from '../testing/run-fixtures';
import { RunResultComponent } from './run-result.component';

/**
 * Every dollar a run's result shows is a string the server wrote, and it is shown as written. 9007199254740993 is the
 * first whole number a double cannot hold: it reads 9007199254740992 once it has passed through a number, and
 * Angular's `currency` pipe, which does pass a string through one, would show it as ...994.00. So a binding that
 * shows this value as it came cannot have gone through a number; one table row for each place the result shows a dollar.
 */
const BIG = '9007199254740993.01';
const SHOWN = '$9,007,199,254,740,993.01';

const fall = (peak: number | null): Drawdown => ({
  fall_usd: BIG,
  fall_ratio: { numerator_usd: BIG, denominator_usd: BIG, rounded_to_6_places: '0.500000' },
  measured_at: 'session_end',
  peak_equity_usd: BIG,
  peak_session_open_ms: peak,
  trough_equity_usd: BIG,
  trough_session_open_ms: sessionOpenMs('2026-09-17'),
});
const day = (row: DayRow): DayRow => ({ ...row, net_profit_usd: BIG, fees_usd: BIG });
const month = (row: MonthRow): MonthRow => ({ ...row, net_profit_usd: BIG, fees_usd: BIG });

/** A complete run in which every dollar the result shows is the value a double cannot hold. */
function runOfBigDollars(): RunDetail {
  return runDetail({
    banner: runBanner({ price_step_usd: BIG }),
    slippage: LADDER.map((point) => ({ ...point, net_profit_usd: BIG })),
    figures: figures({
      net_profit_usd: BIG,
      ending_cash_usd: BIG,
      total_fees_usd: BIG,
      slippage_paid_usd: BIG,
      profit_factor: { numerator_usd: BIG, denominator_usd: BIG, rounded_to_6_places: '1.000000' },
      drawdown_by_dollars: fall(sessionOpenMs('2026-09-15')),
      drawdown_by_fraction: fall(null),
      by_day: DAY_ROWS.map(day),
      by_month: MONTH_ROWS.map(month),
    }),
  });
}

const tile = (name: RegExp | string) => screen.getByText(name, { selector: 'dt' }).closest('.tile') as HTMLElement;
const falls = () => [...screen.getByRole('region', { name: 'Maximum drawdown' }).querySelectorAll<HTMLElement>('.fall')];
const line = (container: HTMLElement | undefined, label: string) => [...(container?.querySelectorAll('li') ?? [])].find((item) => reads(item).startsWith(label)) ?? null;
const column = (name: string, at: number) => [...screen.getByRole('region', { name }).querySelectorAll('tbody tr')].map((row) => reads(row.querySelectorAll('td')[at] ?? null));
const ladder = () => [...screen.getByRole('table', { name: 'The run replayed at each slippage' }).querySelectorAll('tbody tr')].map((row) => reads(row.querySelectorAll('td')[0] ?? null));

/** Each place the result shows a dollar: its name, how to read it off the screen, and what it must read. */
const PLACES: readonly (readonly [string, () => string | string[], string | string[]])[] = [
  ['the banner’s price step', () => reads(screen.getByRole('note', { name: 'How these figures were made' }).querySelectorAll('p')[2] ?? null), `Slippage assumed: 0.3%. Price step: ${SHOWN}.`],
  ['net profit', () => reads(tile('Net profit')), `Net profit ${SHOWN}`],
  ['ending cash', () => reads(tile('Ending cash')), `Ending cash ${SHOWN}`],
  ['total fees', () => reads(tile('Total fees')), `Total fees ${SHOWN}`],
  ['slippage paid', () => reads(tile('Slippage paid')), `Slippage paid ${SHOWN}`],
  ['the profit factor’s gross profit and gross loss', () => reads(tile(/^Profit factor/)), `Profit factor gross profit over gross loss 1.000000 rounded to 6 places · ${SHOWN} over ${SHOWN}`],
  ['the fall of the largest fall in dollars', () => reads(falls()[0]?.querySelector('.big') ?? null), SHOWN],
  ['the fall of the largest fall as a fraction', () => reads(falls()[1]?.querySelector('.big') ?? null), SHOWN],
  ['the peak equity of the largest fall in dollars', () => reads(line(falls()[0], 'Peak equity')), `Peak equity: ${SHOWN} at the end of 2026-09-15`],
  ['the peak equity of the largest fall as a fraction', () => reads(line(falls()[1], 'Peak equity')), `Peak equity: ${SHOWN} before the first session`],
  ['the trough equity of the largest fall in dollars', () => reads(line(falls()[0], 'Trough equity')), `Trough equity: ${SHOWN} at the end of 2026-09-17`],
  ['the trough equity of the largest fall as a fraction', () => reads(line(falls()[1], 'Trough equity')), `Trough equity: ${SHOWN} at the end of 2026-09-17`],
  ['the net profit of each rung of the slippage ladder', ladder, LADDER.map(() => SHOWN)],
  ['the net profit of each day', () => column('By day', 7), DAY_ROWS.map(() => SHOWN)],
  ['the fees of each day', () => column('By day', 8), DAY_ROWS.map(() => SHOWN)],
  ['the net profit of each month', () => column('By month', 7), MONTH_ROWS.map(() => SHOWN)],
  ['the fees of each month', () => column('By month', 8), MONTH_ROWS.map(() => SHOWN)],
];

const occurrences = (text: string) => text.split(SHOWN).length - 1;

describe('the dollars of a result are shown exactly as the server wrote them', () => {
  it.each(PLACES)('%s', async (_place, read, shown) => {
    await render(RunResultComponent, { inputs: { run: runOfBigDollars() } });

    expect(read()).toEqual(shown);
  });

  it('has a row above for every dollar on the screen: nothing else on the result carries one', async () => {
    const { container } = await render(RunResultComponent, { inputs: { run: runOfBigDollars() } });

    const onScreen = reads(container);
    const named = PLACES.map(([, , shown]) => (Array.isArray(shown) ? shown : [shown]).join(' ')).join(' ');
    expect((onScreen.match(/-?\$[\d,]+(\.\d+)?/g) ?? []).filter((amount) => amount !== SHOWN)).toEqual([]);
    expect(occurrences(onScreen)).toBe(occurrences(named));
  });
});
