import { fireEvent, render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

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
  it('has a row for each session: its date in New York dates, how it came out, its trades, its profit and fees as the server wrote them', async () => {
    await render(RunDayTableComponent, { inputs: { rows: DAY_ROWS } });

    expect(bodyRows().map((row) => reads(row))).toEqual([
      '2026-09-14 Traded 2 2 0 0 $150.25 $2.60',
      '2026-09-15 Traded 4 1 3 0 -$62.40 $5.20',
      '2026-09-16 No Trade 0 0 0 0 $0.00 $0.00',
      '2026-09-17 Skipped Window Does Not Fit 0 0 0 0 $0.00 $0.00',
      '2026-09-18 Traded 6 4 1 1 $1,146.71 $39.00',
    ]);
  });

  it('draws every date in New York dates', async () => {
    await render(RunDayTableComponent, { inputs: { rows: DAY_ROWS } });

    expectTradingDatesInNewYork(document.body);
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

  it('has a heading for the table and an accessible name for each column', async () => {
    await render(RunDayTableComponent, { inputs: { rows: DAY_ROWS } });

    expect(screen.getByRole('region', { name: 'By day' })).toBeTruthy();
    expect(screen.getAllByRole('columnheader').map((cell) => reads(cell))).toEqual(['Date', 'Status', 'Skipped because', 'Trades', 'Won', 'Lost', 'Break-even', 'Net profit', 'Fees']);
  });
});

describe('the by-month table', () => {
  it('has a row for each month: named by the year and month, with its sessions by status', async () => {
    await render(RunMonthTableComponent, { inputs: { rows: MONTH_ROWS } });

    expect(bodyRows().map((row) => reads(row))).toEqual([
      '2026-08 3 2 5 3 2 0 $88.10 $6.50 Traded 2, No Trade 1',
      '2026-09 7 6 7 4 2 1 $1,146.46 $40.30 Traded 6, Skipped 1',
    ]);
  });

  it('sorts by a whole-number column, and by the month', async () => {
    await render(RunMonthTableComponent, { inputs: { rows: MONTH_ROWS } });

    fireEvent.click(header('Sessions$'));
    fireEvent.click(header('Sessions$'));
    expect(firstCells()).toEqual(['2026-09', '2026-08']);
    fireEvent.click(header('Month'));
    expect(firstCells()).toEqual(['2026-08', '2026-09']);
  });

  it('does not sort by a dollar column', async () => {
    await render(RunMonthTableComponent, { inputs: { rows: MONTH_ROWS } });

    expect(header('Net profit').getAttribute('aria-sort')).toBeNull();
    expect(within(screen.getByRole('region', { name: 'By month' })).getByRole('table')).toBeTruthy();
  });
});
