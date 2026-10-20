import { fireEvent, render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { etMidnightMs } from '../../../shared/date/et-midnight';
import { sessionOpenMs } from '../../../shared/date-picker/testing/fake-trading-calendar';
import type { DayRow } from '../options-backtest.types';
import { expectTradingDatesInNewYork, reads } from '../testing/page-actions';
import { DAY_ROWS, MONTH_ROWS } from '../testing/run-fixtures';
import { RunDayTableComponent } from './run-day-table.component';
import { RunMonthTableComponent } from './run-month-table.component';

const bodyRows = () => [...document.querySelectorAll('tbody tr')];
const firstCells = () => bodyRows().map((row) => reads(row.querySelector('td')));
const header = (name: string) => screen.getByRole('columnheader', { name: new RegExp(`^${name}`) });

describe('the by-day table', () => {
  it('has a row for each session, draws every date in New York dates, and has a heading for the table and an accessible name for each column', async () => {
    await render(RunDayTableComponent, { inputs: { rows: DAY_ROWS } });

    expect.soft(bodyRows().map((row) => reads(row)), 'has a row for each session: its date in New York dates, how it came out, its trades, its profit and fees as the server wrote them').toEqual([
      '2026-09-14 Traded 2 2 0 0 $150.25 $2.60 Look at',
      '2026-09-15 Traded 4 1 3 0 -$62.40 $5.20 Look at',
      '2026-09-16 No Trade 0 0 0 0 $0.00 $0.00 Look at',
      '2026-09-17 Skipped Window Does Not Fit 0 0 0 0 $0.00 $0.00 Look at',
      '2026-09-18 Traded 6 4 1 1 $1,146.71 $39.00 Look at',
    ]);
    expect.soft(screen.getByRole('region', { name: 'By day' }), 'has a heading for the table').toBeTruthy();
    expect.soft(screen.getAllByRole('columnheader').map((cell) => reads(cell)), 'has an accessible name for each column').toEqual(['Date', 'Status', 'Skipped because', 'Trades', 'Won', 'Lost', 'Break-even', 'Net profit', 'Fees', 'Look at the day']);
    expectTradingDatesInNewYork(document.body);
  });

  // 00:30 New York time on the 16th is 23:30 on the 15th in Chicago and 18:30 on the 15th in Honolulu; 20:00 on the 15th is the 16th in Tokyo.
  it.each([
    ['00:30 on the 16th', etMidnightMs('2026-09-16') + 30 * 60_000, 'Look at 2026-09-16'],
    ['20:00 on the 15th', etMidnightMs('2026-09-15') + 20 * 60 * 60_000, 'Look at 2026-09-15'],
  ])('names the control that opens a day by its New York date, whatever the viewer’s zone: an open at %s', async (_name, openMs, label) => {
    await render(RunDayTableComponent, { inputs: { rows: [{ ...DAY_ROWS[0], session_open_ms: openMs }] } });

    expect(screen.getByRole('button', { name: label })).toBeTruthy();
  });

  it('offers each day to look at closely, named by the day, and tells who asked and which day', async () => {
    const asked: { openMs: number; origin: HTMLElement | null }[] = [];
    await render(RunDayTableComponent, { inputs: { rows: DAY_ROWS }, on: { openDay: (request: { openMs: number; origin: HTMLElement | null }) => asked.push(request) } });

    const buttons = screen.getAllByRole('button', { name: /^Look at / });
    expect(buttons.map((button) => button.getAttribute('aria-label'))).toEqual(['Look at 2026-09-14', 'Look at 2026-09-15', 'Look at 2026-09-16', 'Look at 2026-09-17', 'Look at 2026-09-18']);
    fireEvent.click(buttons[3]);

    expect(asked).toEqual([{ openMs: sessionOpenMs('2026-09-17'), origin: buttons[3] }]);
  });

  it('starts in date order and sorts by a whole-number column when its heading is pressed, both ways', async () => {
    await render(RunDayTableComponent, { inputs: { rows: DAY_ROWS } });
    expect(firstCells()).toEqual(['2026-09-14', '2026-09-15', '2026-09-16', '2026-09-17', '2026-09-18']);

    fireEvent.click(header('Trades'));
    const ascending = bodyRows().map((row) => reads(row.querySelectorAll('td')[3]));
    fireEvent.click(header('Trades'));
    const descending = bodyRows().map((row) => reads(row.querySelectorAll('td')[3]));

    expect(ascending).toEqual(['0', '0', '2', '4', '6']);
    expect(descending).toEqual(['6', '4', '2', '0', '0']);
  });

  it('sorts by date, the other way, when the date heading is pressed', async () => {
    await render(RunDayTableComponent, { inputs: { rows: DAY_ROWS } });

    fireEvent.click(header('Date'));

    expect(firstCells()).toEqual(['2026-09-18', '2026-09-17', '2026-09-16', '2026-09-15', '2026-09-14']);
  });

  it('does not sort by a dollar column: ordering the exact strings would mean reading them as numbers', async () => {
    await render(RunDayTableComponent, { inputs: { rows: DAY_ROWS } });

    expect(header('Net profit').getAttribute('aria-sort')).toBeNull();
    expect(header('Fees').getAttribute('aria-sort')).toBeNull();
    expect(header('Trades').getAttribute('aria-sort')).not.toBeNull();
    fireEvent.click(header('Net profit'));
    expect(firstCells()).toEqual(['2026-09-14', '2026-09-15', '2026-09-16', '2026-09-17', '2026-09-18']);
  });

  it('pages a long run, and does not page a short one', async () => {
    const long: DayRow[] = Array.from({ length: 30 }, (_, index) => ({ ...DAY_ROWS[0], session_open_ms: sessionOpenMs('2026-09-14') + index * 86_400_000 }));

    const { rerender } = await render(RunDayTableComponent, { inputs: { rows: long } });
    expect(bodyRows()).toHaveLength(25);
    expect(document.querySelector('p-paginator, .p-paginator')).not.toBeNull();

    await rerender({ inputs: { rows: DAY_ROWS } });
    expect(bodyRows()).toHaveLength(5);
    expect(document.querySelector('.p-paginator')).toBeNull();
  });
});

describe('the by-month table', () => {
  it('has a row for each month: named by the year and month, with its sessions by status, and does not sort by a dollar column', async () => {
    await render(RunMonthTableComponent, { inputs: { rows: MONTH_ROWS } });

    expect.soft(bodyRows().map((row) => reads(row)), 'has a row for each month: named by the year and month, with its sessions by status').toEqual([
      '2026-08 3 2 5 3 2 0 $88.10 $6.50 Traded 2, No Trade 1',
      '2026-09 7 6 7 4 2 1 $1,146.46 $40.30 Traded 6, Skipped 1',
    ]);
    expect.soft(header('Net profit').getAttribute('aria-sort'), 'does not sort by a dollar column').toBeNull();
    expect.soft(within(screen.getByRole('region', { name: 'By month' })).getByRole('table'), 'holds the table in the By month region').toBeTruthy();
  });

  it('sorts by a whole-number column, and by the month', async () => {
    await render(RunMonthTableComponent, { inputs: { rows: MONTH_ROWS } });

    fireEvent.click(header('Sessions$'));
    fireEvent.click(header('Sessions$'));
    expect(firstCells()).toEqual(['2026-09', '2026-08']);
    fireEvent.click(header('Month'));
    expect(firstCells()).toEqual(['2026-08', '2026-09']);
  });
});
