import { screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { reads } from '../../testing/page-actions';
import type { SessionDetail } from '../run-detail.types';
import { sessionDetail, TUESDAY_OPEN } from '../testing/detail-fixtures';
import { fakeRunDetail } from '../testing/fake-run-detail';
import { renderSession } from '../testing/render-session';
import { owedSession, spreadSession } from '../testing/selling-fixtures';

vi.mock('lightweight-charts', () => ({
  createSeriesMarkers: vi.fn(),
  LineSeries: 'LineSeries',
  LineStyle: { Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3, SparseDotted: 4 },
  TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
}));

/**
 * A covered put spread read on the one-day view (#3109): a put 580 bought to cover a put 585 sold, in the trades of the
 * day, in the lists that are the chart as text, and in the legend of each pane. A sold leg is never read as a bought one.
 */

const ASKED = { openMs: TUESDAY_OPEN, origin: null };

function servedAs(day: SessionDetail) {
  return fakeRunDetail(vi, { session: vi.fn(async () => day) });
}

const tableRows = (name: string) => [...within(screen.getByRole('region', { name })).getAllByRole('row')].slice(1).map((row) => [...row.querySelectorAll('td')].map((cell) => reads(cell)));
const columnNames = (name: string) => within(screen.getByRole('region', { name })).getAllByRole('columnheader').map((header) => reads(header));
const panes = () => [...screen.getByRole('list', { name: 'Panes of the chart, from the top' }).querySelectorAll(':scope > li')].map((pane) => [...pane.querySelectorAll('.entries li')].map((entry) => reads(entry)));

describe('a covered spread that closed, on the one-day view', () => {
  it('says in the trades of the day that the sold leg was sold to open and bought back, and says nothing more of the bought one', async () => {
    await renderSession(vi, { detail: servedAs(spreadSession()), request: ASKED });
    await screen.findByRole('table', { name: 'Leg trades of this day' });

    const rows = tableRows('Leg trades of this day (scrolls sideways)');
    // The bought leg's cells are what they have always been: its entry is a try and its exit a reason.
    expect(rows[0].slice(1, 3)).toEqual(['long_put', 'first entry']);
    expect(rows[0][12]).toBe('Exit Time');
    expect(rows[1].slice(1, 3)).toEqual(['short_put', 'first entry Sold to open']);
    expect(rows[1][12]).toBe('Stop Bought back');
    // The sold leg's stop level is above the price it was sold for.
    expect(rows[1].slice(7, 12)).toEqual(['$2.20', '$2.18', '— none', '— none', '$3.27']);
  });

  it('lists each fill by the side it traded on, and heads the reason column by what the day holds', async () => {
    await renderSession(vi, { detail: servedAs(spreadSession()), request: ASKED });
    await screen.findByRole('region', { name: 'Fills (scrolls sideways)' });

    expect(tableRows('Fills (scrolls sideways)').map((row) => [row[0], row[1], row[2], row[6], row[8]])).toEqual([
      ['Bought', 'up arrow', 'long_put, first entry', '$1.42', ''],
      ['Sold to open', 'down arrow', 'short_put, first entry', '$2.18', ''],
      ['Bought back', 'up arrow', 'short_put, first entry', '$3.32', 'Stop'],
      ['Sold', 'down arrow', 'long_put, first entry', '$1.12', 'Exit Time'],
    ]);
    // A buy-back is not a sale, so a day with one does not head the column "Sold because".
    expect(columnNames('Fills (scrolls sideways)').at(-1)).toBe('Closed because');
  });

  it('names each pane’s legend for the way its fills traded, with the glyph of its arrow', async () => {
    await renderSession(vi, { detail: servedAs(spreadSession()), request: ASKED });
    await screen.findByRole('list', { name: 'Panes of the chart, from the top' });

    expect(panes()).toEqual([
      ['Minute closes (solid line)'],
      ['Minute closes (solid line)', '▲ Bought (up arrow)', '▼ Sold (down arrow)'],
      ['Minute closes (solid line)', '▼ Sold to open (down arrow)', '▲ Bought back (up arrow)', 'Stop level (long dashes)'],
    ]);
  });

  it('lists the sold leg’s stop level above its sale, between the minute it filled and the minute the buy-back was decided', async () => {
    await renderSession(vi, { detail: servedAs(spreadSession()), request: ASKED });
    await screen.findByRole('region', { name: 'Levels (scrolls sideways)' });

    expect(tableRows('Levels (scrolls sideways)').map((row) => [row[0], row[2], row[4], row[5], row[6]])).toEqual([
      ['Stop level', 'short_put, first entry', '$3.27', '09:34:00 ET', '09:39:00 ET'],
    ]);
  });

  it('keeps the section of marks as it is for a day that has none, with the words of a bought leg', async () => {
    await renderSession(vi, { detail: servedAs(spreadSession()), request: ASKED });

    expect(await screen.findByText('No leg was left unsold on this day.')).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Marked, not sold' })).toBeTruthy();
  });
});

describe('a covered spread that ended marked, on the one-day view', () => {
  it('lists the cover as not sold and the sold leg as owed, in a section that names both', async () => {
    await renderSession(vi, { detail: servedAs(owedSession()), request: ASKED });
    await screen.findByRole('region', { name: 'Marks (scrolls sideways)' });

    expect(screen.getByRole('heading', { name: 'Marked, not sold or owed' })).toBeTruthy();
    expect(tableRows('Marks (scrolls sideways)').map((row) => [row[0], row[1], row[2], row[5]])).toEqual([
      ['Marked, not sold', 'square', 'long_put, first entry', '$1.00'],
      ['Marked, owed', 'square', 'short_put, first entry', '$2.40'],
    ]);
  });

  it('says in the trades of the day that the sold leg’s mark is owed and the cover’s is not sold', async () => {
    await renderSession(vi, { detail: servedAs(owedSession()), request: ASKED });
    await screen.findByRole('table', { name: 'Leg trades of this day' });

    const marked = tableRows('Leg trades of this day (scrolls sideways)').map((row) => row[14]);
    expect(marked).toEqual([
      'Marked at $1.00, not sold, last traded in the minute ending 09:40 ET',
      'Marked at $2.40, owed, not bought back, last traded in the minute ending 09:40 ET',
    ]);
  });

  it('does not list a mark among the fills, and heads the reason column by the sale to open the day holds', async () => {
    await renderSession(vi, { detail: servedAs(owedSession()), request: ASKED });
    await screen.findByRole('region', { name: 'Fills (scrolls sideways)' });

    expect(tableRows('Fills (scrolls sideways)').map((row) => row[0])).toEqual(['Bought', 'Sold to open']);
    expect(columnNames('Fills (scrolls sideways)').at(-1)).toBe('Closed because');
  });

  it('says in the legend of each pane which mark it holds', async () => {
    await renderSession(vi, { detail: servedAs(owedSession()), request: ASKED });
    await screen.findByRole('list', { name: 'Panes of the chart, from the top' });

    expect(panes().map((entries) => entries.filter((entry) => entry.startsWith('■')))).toEqual([
      [],
      ['■ Marked, not sold (square)'],
      ['■ Marked, owed (square)'],
    ]);
  });
});

describe('a day of bought legs, on the one-day view', () => {
  it('reads as it always did: the same fill words, the same heading, the same marks section and no word of a sold leg', async () => {
    await renderSession(vi, { detail: servedAs(sessionDetail()), request: ASKED });
    await screen.findByRole('region', { name: 'Fills (scrolls sideways)' });

    expect(tableRows('Fills (scrolls sideways)').map((row) => row[0])).toEqual(['Bought', 'Bought', 'Sold']);
    expect(columnNames('Fills (scrolls sideways)').at(-1)).toBe('Sold because');
    expect(screen.getByRole('heading', { name: 'Marked, not sold' })).toBeTruthy();
    expect(tableRows('Leg trades of this day (scrolls sideways)').map((row) => row[2])).toEqual(['first entry', 'first entry']);
    expect(document.body.textContent).not.toMatch(/Sold to open|Bought back|owed/);
  });
});
