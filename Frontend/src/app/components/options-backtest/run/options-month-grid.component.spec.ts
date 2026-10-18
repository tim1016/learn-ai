import { fireEvent, render, screen, within } from '@testing-library/angular';
import { expect, it } from 'vitest';

import { OptionsMonthGridComponent, type MonthCell, type MonthMetric } from './options-month-grid.component';

const metrics: readonly MonthMetric[] = [
  { key: 'pnl', label: 'Net P&L', unit: 'usd', signed: true },
  { key: 'trades', label: 'Trades', unit: 'count', signed: false },
];
const cells: readonly MonthCell[] = [
  { year: 2026, month: 2, values: { pnl: '9007199254740993.01', trades: '31' } },
  { year: 2025, month: 12, values: { pnl: '-125.25', trades: '3' } },
  { year: 2026, month: 1, values: { pnl: '0', trades: '0' } },
];

it('aligns twelve months across ordered year rows and distinguishes absent months from zero', async () => {
  await render(OptionsMonthGridComponent, { inputs: { cells, metrics } });
  const grid = screen.getByRole('grid', { name: 'Results by year and month' });
  expect(within(grid).getAllByRole('rowheader').map((year) => year.textContent?.trim())).toEqual(['2025', '2026']);
  const year = within(grid).getByRole('rowheader', { name: '2026' }).closest<HTMLElement>('[role=row]');
  if (year === null) throw new Error('The year row is missing.');
  const months = within(year).getAllByRole('gridcell');
  expect(months).toHaveLength(12);
  expect(within(months[0]).getByRole('button', { name: 'Jan 2026, Net P&L: 0' }).textContent).toContain('$0.00');
  expect(within(months[1]).getByRole('button', { name: 'Feb 2026, Net P&L: 9007199254740993.01' }).textContent).toContain('$9,007,199,254,740,993.01');
  expect(months[2].textContent?.trim()).toBe('—');
  expect(within(months[2]).queryByRole('button')).toBeNull();
});

it('switches the quantity and inspects the same month’s exact P&L and trade count', async () => {
  await render(OptionsMonthGridComponent, { inputs: { cells, metrics } });
  fireEvent.click(screen.getByRole('button', { name: 'Trades' }));
  const february = screen.getByRole('button', { name: 'Feb 2026, Trades: 31' });
  expect(february.textContent?.trim()).toBe('31');
  fireEvent.click(february);
  const detail = within(screen.getByRole('region', { name: 'Selected month' }));
  expect(detail.getByRole('heading', { name: 'Feb 2026' })).toBeTruthy();
  expect(detail.getByText('$9,007,199,254,740,993.01')).toBeTruthy();
  expect(detail.getByText('31')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Dec 2025, Trades: 3' }).textContent?.trim()).toBe('3');
});
