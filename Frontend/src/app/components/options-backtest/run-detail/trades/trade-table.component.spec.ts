import userEvent from '@testing-library/user-event';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { etMidnightMs } from '../../../../shared/date/et-midnight';
import { reads } from '../../testing/page-actions';
import type { DayRequest } from '../day-request';
import type { LegTrade } from '../run-detail.types';
import { FOUR_TRADES, markedTrade, stoppedTrade, TUESDAY_OPEN } from '../testing/detail-fixtures';
import { coverTrade, owedTrade, soldTrade } from '../testing/selling-fixtures';
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
  it('is a real table with a caption and twenty columns, and shows each trade with each column from its own field', async () => {
    await renderTable();

    expect.soft(screen.getByRole('table', { name: 'Leg trades' }), 'is a real table with a caption').toBeTruthy();
    const region = screen.getByRole('region', { name: 'Leg trades (scrolls sideways)' });
    expect.soft(region.getAttribute('tabindex'), 'its region scrolls and is reachable by keyboard').toBe('0');
    expect.soft(screen.getAllByRole('columnheader').map((header) => reads(header)), 'names its twenty columns').toEqual([
      'Day', 'Leg', 'Entry', 'Contract', 'Contracts',
      'Entry decided', 'Entry filled in the minute ending', 'Entry price seen', 'Entry price filled',
      'Starting price', 'Momentum level', 'Stop level',
      'Left because', 'Exit decided', 'Exit filled in the minute ending', 'Exit price seen', 'Exit price filled',
      'Entry fee', 'Exit fee', 'Profit',
    ]);

    const stopped = cells(bodyRows()[0]);
    expect.soft(stopped[0], 'shows a trade that was bought on momentum and stopped out, each column from its own field (the day)').toBe('2026-09-15');
    expect.soft(stopped.slice(1, 3), 'shows a trade that was bought on momentum and stopped out, each column from its own field (the leg and the try)').toEqual(['call', 'first entry']);
    expect.soft(stopped[3], 'shows a trade that was bought on momentum and stopped out, each column from its own field (the contract)').toContain("Call 585 Oct 16 '26");
    expect.soft(stopped.slice(4), 'shows a trade that was bought on momentum and stopped out, each column from its own field (the rest)').toEqual([
      '1',
      '09:37:00 ET', '09:38 ET', '$2.55', '$2.57',
      '$2.00', '$2.50', '$1.93',
      'Stop', '09:40:00 ET', '09:41 ET', '$1.84', '$1.82',
      '$0.65', '$0.65', '-$76.30',
    ]);

    const reentry = cells(bodyRows()[1]);
    expect.soft(reentry.slice(1, 3), 'shows a re-entry as one (the leg and the try)').toEqual(['call', 're-entry 1']);
    expect.soft(reentry[3], 'shows a re-entry as one (the contract)').toContain("Call 586 Oct 16 '26");
    expect.soft(reentry.slice(9, 12), 'shows a re-entry with the stop it carries and none of the momentum figures it never had').toEqual(['— none', '— none', '$1.20']);
    expect.soft(reentry.slice(12, 17), 'shows a re-entry with the way it left').toEqual(['Exit Time', '15:55:00 ET', '15:56 ET', '$1.90', '$1.88']);
    expect.soft(reentry[19], 'shows a re-entry with its profit').toBe('$26.70');

    const markedRow = bodyRows()[2];
    const marked = cells(markedRow);
    expect.soft(marked.slice(1, 3), 'shows a leg that was marked and not sold (the leg and the try)').toEqual(['put', 'first entry']);
    expect.soft(marked[4], 'shows a leg that was marked and not sold (the contracts)').toBe('2');
    expect.soft(marked.slice(12, 14), 'shows a leg that was marked and not sold with the mark in the sale’s columns').toEqual(['Marked Not Sold', '16:00:00 ET']);
    expect.soft(marked[14], 'shows a leg that was marked and not sold with a mark in words').toBe('Marked at $1.25, not sold, last traded in the minute ending 15:58 ET');
    expect.soft(markedRow.querySelectorAll('td')[14].getAttribute('colspan'), 'shows the mark in words across the sale’s three columns').toBe('3');
    // The fees and the profit follow the mark in the same columns as for a sale.
    expect.soft(marked.slice(15), 'shows the fees and the profit after the mark').toEqual(['$1.30', '$0.00', '-$55.30']);
    // Nothing of a sale: the row is three cells shorter than a sold trade's.
    expect.soft(markedRow.querySelectorAll('td').length, 'shows no sale for a leg that was marked and not sold').toBe(cells(bodyRows()[0]).length - 2);

    const secondDay = cells(bodyRows()[3]);
    expect.soft(secondDay[0], 'shows the second day’s trade under its own date').toBe('2026-09-16');
    expect.soft(secondDay.slice(1, 3), 'shows the second day’s trade (the leg and the try)').toEqual(['put', 'first entry']);
    expect.soft(secondDay.slice(5, 9), 'shows the second day’s trade (its entry)').toEqual(['09:35:00 ET', '09:36 ET', '$3.10', '$3.12']);
    expect.soft(secondDay.slice(12, 17), 'shows the second day’s trade (its exit)').toEqual(['Exit Time', '15:54:00 ET', '15:55 ET', '$3.40', '$3.38']);
    expect.soft(secondDay[19], 'shows the second day’s trade (its profit)').toBe('$24.70');
  });

  it('says when a figure is absent, in a word a screen reader reads, and not as a zero', async () => {
    await renderTable({ trades: [stoppedTrade({ starting_price_usd: null, momentum_level_usd: null, stop_level_usd: null })] });

    expect(cells(bodyRows()[0]).slice(9, 12)).toEqual(['— none', '— none', '— none']);
    expect(bodyRows()[0].querySelectorAll('.none[aria-hidden="true"]').length).toBe(3);
  });

  it('keeps the trades in the order it was given', async () => {
    await renderTable({ trades: [FOUR_TRADES[3], FOUR_TRADES[0]] });

    expect(bodyRows().map((row) => cells(row)[0])).toEqual(['2026-09-16', '2026-09-15']);
  });

  it('reveals the broker code when hovering a trade’s contract', async () => {
    await renderTable({ trades: [stoppedTrade()] });

    expect(screen.queryByText('SPY261016C00585000')).toBeNull();
    await userEvent.hover(screen.getByLabelText("SPY Oct 16 '26 585 Call"));

    expect(await screen.findByText('SPY261016C00585000')).toBeTruthy();
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

describe('the table of leg trades, a sold leg (#3109)', () => {
  it('says a sold leg was sold to open and bought back, in the cells that say how a leg entered and why it left', async () => {
    await renderTable({ trades: [coverTrade(), soldTrade()] });

    const [cover, sold] = bodyRows().map((row) => cells(row));
    // The bought leg's cells are the ones a bought leg has always had.
    expect(cover.slice(1, 3)).toEqual(['long_put', 'first entry']);
    expect(cover[12]).toBe('Exit Time');
    expect(sold.slice(1, 3)).toEqual(['short_put', 'first entry Sold to open']);
    expect(sold[12]).toBe('Stop Bought back');
    // The buy-back is the exit, with its own prices and minute; the stop level is above the price it was sold for.
    expect(sold.slice(7, 12)).toEqual(['$2.20', '$2.18', '— none', '— none', '$3.27']);
    expect(sold.slice(13, 17)).toEqual(['09:39:00 ET', '09:40 ET', '$3.30', '$3.32']);
    expect(sold[19]).toBe('-$114.00');
  });

  it('says a sold leg’s mark is owed and not bought back, where a bought leg’s is not sold', async () => {
    await renderTable({ trades: [markedTrade(), owedTrade()] });

    const [bought, sold] = bodyRows().map((row) => cells(row));
    expect(bought[14]).toBe('Marked at $1.25, not sold, last traded in the minute ending 15:58 ET');
    expect(sold[12]).toBe('Marked Not Bought Back');
    expect(sold[14]).toBe('Marked at $2.40, owed, not bought back, last traded in the minute ending 09:40 ET');
    expect(sold[2]).toBe('first entry Sold to open');
  });

  it('adds no word to a bought leg’s row', async () => {
    await renderTable({ trades: [...FOUR_TRADES] });

    expect(document.body.textContent).not.toMatch(/Sold to open|Bought back|owed/);
    expect(bodyRows().every((row) => row.querySelector('.side') === null)).toBe(true);
  });
});
