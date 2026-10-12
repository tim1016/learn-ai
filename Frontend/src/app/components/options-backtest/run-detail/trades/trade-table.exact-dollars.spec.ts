import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { reads } from '../../testing/page-actions';
import type { LegTrade } from '../run-detail.types';
import { markedTrade, stoppedTrade } from '../testing/detail-fixtures';
import { TradeTableComponent } from './trade-table.component';

/**
 * Every price, fee and profit of a trade is a string the server wrote, and the table shows it as written.
 * 9007199254740993.01 is the first whole number-and-cents a double cannot hold: it reads ...992 once it has passed through
 * a number. A value with four decimals is not an amount in cents, and is shown as it came, not rounded to two places.
 * One row below for each place the table shows a price, so a binding added later without a row fails the count.
 */
const BIG = '9007199254740993.01';
const SHOWN = '$9,007,199,254,740,993.01';
const FOUR = '1.2345';

function sold(value: string): LegTrade {
  return stoppedTrade({
    starting_price_usd: value,
    momentum_level_usd: value,
    stop_level_usd: value,
    entry: { bar_close_ms: stoppedTrade().entry.bar_close_ms, seen_usd: value, filled_usd: value },
    sale: { bar_close_ms: stoppedTrade().sale?.bar_close_ms ?? 0, seen_usd: value, filled_usd: value },
    entry_fee_usd: value,
    exit_fee_usd: value,
    pnl_usd: value,
  });
}

function marked(value: string): LegTrade {
  return markedTrade({
    starting_price_usd: value,
    momentum_level_usd: value,
    stop_level_usd: value,
    entry: { bar_close_ms: markedTrade().entry.bar_close_ms, seen_usd: value, filled_usd: value },
    mark: { bar_close_ms: markedTrade().mark?.bar_close_ms ?? 0, price_usd: value },
    entry_fee_usd: value,
    exit_fee_usd: value,
    pnl_usd: value,
  });
}

/** Each place a sold trade's row shows a price: its name and the cell (counted from 0) that holds it. */
const SOLD_PLACES: readonly (readonly [string, number])[] = [
  ['entry price seen', 7],
  ['entry price filled', 8],
  ['starting price', 9],
  ['momentum level', 10],
  ['stop level', 11],
  ['exit price seen', 15],
  ['exit price filled', 16],
  ['entry fee', 17],
  ['exit fee', 18],
  ['profit', 19],
];

/** The same for a leg marked and not sold: its mark is one cell across the sale's three columns, so the rest sit three places earlier. */
const MARKED_PLACES: readonly (readonly [string, number])[] = [
  ['entry price seen', 7],
  ['entry price filled', 8],
  ['starting price', 9],
  ['momentum level', 10],
  ['stop level', 11],
  ['the price it was marked at', 14],
  ['entry fee', 15],
  ['exit fee', 16],
  ['profit', 17],
];

async function cellsOf(trade: LegTrade): Promise<{ cells: string[]; amounts: string[] }> {
  const { container, fixture } = await render(TradeTableComponent, { inputs: { trades: [trade], caption: 'Leg trades' } });
  const cells = [...screen.getAllByRole('row')[1].querySelectorAll('td')].map((cell) => reads(cell));
  const amounts = reads(container).match(/-?\$[\d,]+(\.\d+)?/g) ?? [];
  fixture.destroy();
  return { cells, amounts };
}

describe.each([
  ['a sold trade', sold, SOLD_PLACES],
  ['a trade marked and not sold', marked, MARKED_PLACES],
] as const)('the prices of %s are shown exactly as the server wrote them', (_name, make, places) => {
  it.each(places)('%s, a value a float cannot hold', async (_place, at) => {
    const { cells } = await cellsOf(make(BIG));

    expect(cells[at]).toContain(SHOWN);
    expect(cells[at]).not.toContain('9,007,199,254,740,992');
  });

  it.each(places)('%s, a value with four decimals', async (_place, at) => {
    const { cells } = await cellsOf(make(FOUR));

    expect(cells[at]).toContain(FOUR);
    expect(cells[at]).not.toContain('$1.23');
  });

  it('has a row above for every price on the screen: the count of dollar amounts is the count of rows, and each is the value', async () => {
    const { amounts } = await cellsOf(make(BIG));

    expect(amounts).toEqual(Array(places.length).fill(SHOWN));
  });
});
