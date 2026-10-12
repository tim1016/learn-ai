import { screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { reads } from '../../testing/page-actions';
import type { SessionDetail } from '../run-detail.types';
import { sessionDetail, TUESDAY_OPEN } from '../testing/detail-fixtures';
import { fakeRunDetail } from '../testing/fake-run-detail';
import { renderSession } from '../testing/render-session';

vi.mock('lightweight-charts', () => ({
  createSeriesMarkers: vi.fn(),
  LineSeries: 'LineSeries',
  LineStyle: { Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3, SparseDotted: 4 },
  TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
}));

/**
 * Every price, cash figure and profit of a day is a string the server wrote, and it is shown as written. 9007199254740993.01
 * is the first whole number-and-cents a double cannot hold; a value with four decimals is not an amount in cents and is
 * shown as it came. One row for each place the day shows a price, so a binding added later without a row fails the count.
 * The day’s own trades are left out: the trade table’s spec holds each of their cells.
 */
const BIG = '9007199254740993.01';
const SHOWN = '$9,007,199,254,740,993.01';
const FOUR = '1.2345';

function dayOf(value: string): SessionDetail {
  const day = sessionDetail();
  return {
    ...day,
    trades: [],
    cash_before_usd: value,
    cash_after_usd: value,
    profit_usd: value,
    events: day.events.map((each) => (each.price_usd == null ? each : { ...each, price_usd: value })),
    levels: day.levels.map((each) => ({ ...each, price_usd: value })),
    fills: day.fills.map((each) => ({ ...each, filled_usd: value, seen_usd: value })),
    marks: day.marks.map((each) => ({ ...each, price_usd: value })),
  };
}

async function open(value: string) {
  const rendered = await renderSession(vi, { detail: fakeRunDetail(vi, { session: vi.fn(async () => dayOf(value)) }), request: { openMs: TUESDAY_OPEN, origin: null } });
  await screen.findByRole('region', { name: 'Marks (scrolls sideways)' });
  return rendered;
}

const fact = (name: string) => reads([...document.querySelectorAll('.facts > div')].find((each) => reads(each).startsWith(name)) ?? null);
const column = (region: string, at: number) =>
  [...within(screen.getByRole('region', { name: region })).getAllByRole('row')].slice(1).map((row) => reads(row.querySelectorAll('td')[at] ?? null));

/** Each place the day shows a price: its name, how to read it off the screen, and how many such places there are. */
const PLACES: readonly (readonly [string, () => string | string[], number])[] = [
  ['the cash before', () => fact('Cash before'), 1],
  ['the cash after', () => fact('Cash after'), 1],
  ['the profit', () => fact('Profit'), 1],
  ['the price of each event', () => column('What happened, in order (scrolls sideways)', 4).filter((text) => text !== ''), 7],
  ['the price of each level', () => column('Levels (scrolls sideways)', 4), 3],
  ['the price each fill filled at', () => column('Fills (scrolls sideways)', 6), 3],
  ['the price seen at each fill', () => column('Fills (scrolls sideways)', 7), 3],
  ['the price each leg was marked at', () => column('Marks (scrolls sideways)', 5), 1],
];

describe('the prices of a day are shown exactly as the server wrote them', () => {
  it.each(PLACES)('%s, a value a float cannot hold', async (_place, read, count) => {
    await open(BIG);

    const shown = read();
    const all = Array.isArray(shown) ? shown : [shown];
    expect(all.length).toBe(count);
    for (const text of all) {
      expect(text).toContain(SHOWN);
      expect(text).not.toContain('9,007,199,254,740,992');
    }
  });

  it.each(PLACES)('%s, a value with four decimals', async (_place, read, count) => {
    await open(FOUR);

    const shown = read();
    const all = Array.isArray(shown) ? shown : [shown];
    expect(all.length).toBe(count);
    for (const text of all) {
      expect(text).toContain(FOUR);
      expect(text).not.toContain('$1.23');
    }
  });

  it('has a row above for every price on the screen: no other place shows a dollar amount', async () => {
    const { view } = await open(BIG);

    const amounts = reads(view.container).match(/-?\$[\d,]+(\.\d+)?/g) ?? [];
    expect(amounts).toEqual(Array(PLACES.reduce((total, [, , count]) => total + count, 0)).fill(SHOWN));
  });
});
