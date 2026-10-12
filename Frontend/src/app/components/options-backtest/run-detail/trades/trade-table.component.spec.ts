import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { etMidnightMs } from '../../../../shared/date/et-midnight';
import { reads } from '../../testing/page-actions';
import type { DayRequest } from '../day-request';
import type { LegTrade } from '../run-detail.types';
import { FOUR_TRADES, markedTrade, stoppedTrade, TUESDAY_OPEN } from '../testing/detail-fixtures';
import { TradeTableComponent } from './trade-table.component';

async function renderTable(options: { trades?: readonly LegTrade[]; canOpenDay?: boolean } = {}) {
  const asked: DayRequest[] = [];
  const view = await render(TradeTableComponent, {
    inputs: { trades: options.trades ?? FOUR_TRADES, caption: 'Leg trades', canOpenDay: options.canOpenDay ?? true },
    on: { openDay: (request: DayRequest) => asked.push(request) },
  });
  return { ...view, asked };
}

const bodyRows = () => [...screen.getAllByRole('row')].slice(1);
const cells = (row: HTMLElement) => [...row.querySelectorAll('td')].map((cell) => reads(cell));

describe('the table of leg trades, its columns', () => {
  it('is a real table with a caption, and its region scrolls and is reachable by keyboard', async () => {
    await renderTable();

    expect(screen.getByRole('table', { name: 'Leg trades' })).toBeTruthy();
    const region = screen.getByRole('region', { name: 'Leg trades (scrolls sideways)' });
    expect(region.getAttribute('tabindex')).toBe('0');
  });

  it('names its twenty columns', async () => {
    await renderTable();

    expect(screen.getAllByRole('columnheader').map((header) => reads(header))).toEqual([
      'Day', 'Leg', 'Entry', 'Contract', 'Contracts',
      'Entry decided', 'Entry filled in the minute ending', 'Entry price seen', 'Entry price filled',
      'Starting price', 'Momentum level', 'Stop level',
      'Left because', 'Exit decided', 'Exit filled in the minute ending', 'Exit price seen', 'Exit price filled',
      'Entry fee', 'Exit fee', 'Profit',
    ]);
  });

  it('shows a trade that was bought on momentum and stopped out, each column from its own field', async () => {
    await renderTable();

    const row = cells(bodyRows()[0]);
    expect(row[0]).toBe('2026-09-15');
    expect(row.slice(1, 3)).toEqual(['call', 'first entry']);
    expect(row[3]).toContain("Oct 16 '26 585 Call");
    expect(row.slice(4)).toEqual([
      '1',
      '09:37:00 ET', '09:38 ET', '$2.55', '$2.57',
      '$2.00', '$2.50', '$1.93',
      'Stop', '09:40:00 ET', '09:41 ET', '$1.84', '$1.82',
      '$0.65', '$0.65', '-$76.30',
    ]);
  });

  it('shows a re-entry as one, with the stop it carries and none of the momentum figures it never had', async () => {
    await renderTable();

    const row = cells(bodyRows()[1]);
    expect(row.slice(1, 3)).toEqual(['call', 're-entry 1']);
    expect(row[3]).toContain("Oct 16 '26 586 Call");
    expect(row.slice(9, 12)).toEqual(['— none', '— none', '$1.20']);
    expect(row.slice(12, 17)).toEqual(['Exit Time', '15:55:00 ET', '15:56 ET', '$1.90', '$1.88']);
    expect(row[19]).toBe('$26.70');
  });

  it('says when a figure is absent, in a word a screen reader reads, and not as a zero', async () => {
    await renderTable({ trades: [stoppedTrade({ starting_price_usd: null, momentum_level_usd: null, stop_level_usd: null })] });

    expect(cells(bodyRows()[0]).slice(9, 12)).toEqual(['— none', '— none', '— none']);
    expect(bodyRows()[0].querySelectorAll('.none[aria-hidden="true"]').length).toBe(3);
  });

  it('shows a leg that was marked and not sold with a mark in words across the sale’s columns, and no sale', async () => {
    await renderTable();

    const row = bodyRows()[2];
    const marked = cells(row);
    expect(marked.slice(1, 3)).toEqual(['put', 'first entry']);
    expect(marked[4]).toBe('2');
    expect(marked.slice(12, 14)).toEqual(['Marked Not Sold', '16:00:00 ET']);
    expect(marked[14]).toBe('Marked at $1.25, not sold, last traded in the minute ending 15:58 ET');
    expect(row.querySelectorAll('td')[14].getAttribute('colspan')).toBe('3');
    // The fees and the profit follow the mark in the same columns as for a sale.
    expect(marked.slice(15)).toEqual(['$1.30', '$0.00', '-$55.30']);
    // Nothing of a sale: the row is three cells shorter than a sold trade's.
    expect(row.querySelectorAll('td').length).toBe(cells(bodyRows()[0]).length - 2);
  });

  it('shows the second day’s trade under its own date', async () => {
    await renderTable();

    const row = cells(bodyRows()[3]);
    expect(row[0]).toBe('2026-09-16');
    expect(row.slice(1, 3)).toEqual(['put', 'first entry']);
    expect(row.slice(5, 9)).toEqual(['09:35:00 ET', '09:36 ET', '$3.10', '$3.12']);
    expect(row.slice(12, 17)).toEqual(['Exit Time', '15:54:00 ET', '15:55 ET', '$3.40', '$3.38']);
    expect(row[19]).toBe('$24.70');
  });

  it('keeps the trades in the order it was given', async () => {
    await renderTable({ trades: [FOUR_TRADES[3], FOUR_TRADES[0]] });

    expect(bodyRows().map((row) => cells(row)[0])).toEqual(['2026-09-16', '2026-09-15']);
  });

  it('puts the OCC symbol one click away from the contract in words', async () => {
    await renderTable({ trades: [stoppedTrade()] });

    expect(screen.queryByText('SPY261016C00585000')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: "Show contract code for SPY Oct 16 '26 585 Call" }));

    expect(screen.getByText('SPY261016C00585000')).toBeTruthy();
  });
});

describe('the table of leg trades, time', () => {
  it('shows every day in New York dates and every time of day in New York time, whatever the viewer’s zone', async () => {
    const { container } = await renderTable();

    const modes = [...container.querySelectorAll('app-timestamp-display')].map((stamp) => stamp.getAttribute('data-timestamp-mode'));
    expect(modes.length).toBeGreaterThan(0);
    expect(modes.every((mode) => mode === 'et' || mode === 'date-et')).toBe(true);
    expect(modes.filter((mode) => mode === 'date-et').length).toBe(4);
  });

  // The order is decided at the close of the 09:37 bar and fills in the next minute, the one that ends 09:38: the table must not read as a fill at 09:38:00 sharp.
  it('says that a fill’s time is the end of the minute it filled in, shown to the minute, while a decision’s time is an instant, shown to the second', async () => {
    await renderTable({ trades: [stoppedTrade()] });

    const headers = screen.getAllByRole('columnheader').map((header) => reads(header));
    const row = cells(bodyRows()[0]);
    const under = (header: RegExp) => row[headers.findIndex((name) => header.test(name))];
    expect(headers.filter((name) => /^(Entry|Exit) filled in the minute ending$/.test(name))).toHaveLength(2);
    expect([under(/^Entry decided/), under(/^Exit decided/)]).toEqual(['09:37:00 ET', '09:40:00 ET']);
    expect([under(/^Entry filled/), under(/^Exit filled/)]).toEqual(['09:38 ET', '09:41 ET']);
  });

  // 00:30 New York time on the 16th is 23:30 on the 15th in Chicago and 18:30 on the 15th in Honolulu.
  it('names the control that opens a trade’s day by the New York date, though that instant is still the day before in Chicago', async () => {
    await renderTable({ trades: [stoppedTrade({ session_open_ms: etMidnightMs('2026-09-16') + 30 * 60_000 })] });

    expect(screen.getByRole('button', { name: 'Look at 2026-09-16, leg call, first entry' })).toBeTruthy();
    expect(cells(bodyRows()[0])[0]).toBe('2026-09-16');
  });

  it('puts a trade on the date its session opened in New York, though that instant is already the next day in Tokyo', async () => {
    // 20:00 New York on the 15th is the 16th in Tokyo; a session's open is never that late, but the date must not follow the viewer.
    await renderTable({ trades: [stoppedTrade({ session_open_ms: Date.UTC(2026, 8, 16, 0, 0, 0) })] });

    expect(cells(bodyRows()[0])[0]).toBe('2026-09-15');
  });
});

describe('the table of leg trades, opening a day', () => {
  it('offers each row’s day to open, named by the day and the trade, and tells who asked', async () => {
    const { asked } = await renderTable({ trades: [FOUR_TRADES[0], FOUR_TRADES[1]] });

    const first = screen.getByRole('button', { name: 'Look at 2026-09-15, leg call, first entry' });
    const second = screen.getByRole('button', { name: 'Look at 2026-09-15, leg call, re-entry 1' });
    fireEvent.click(second);

    expect(asked).toEqual([{ openMs: TUESDAY_OPEN, origin: second }]);
    expect(first).not.toBe(second);
  });

  it('offers none when the table is already inside a day', async () => {
    await renderTable({ canOpenDay: false });

    expect(screen.queryByRole('button', { name: /^Look at / })).toBeNull();
    expect(cells(bodyRows()[0])[0]).toBe('2026-09-15');
  });

  it('is told again each time the same day is asked for', async () => {
    const listener = vi.fn();
    await render(TradeTableComponent, { inputs: { trades: [markedTrade()], caption: 'Leg trades' }, on: { openDay: listener } });
    const button = screen.getByRole('button', { name: /^Look at / });

    fireEvent.click(button);
    fireEvent.click(button);

    expect(listener).toHaveBeenCalledTimes(2);
    expect(listener.mock.calls[0][0]).not.toBe(listener.mock.calls[1][0]);
  });
});
