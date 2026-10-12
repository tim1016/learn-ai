import { describe, expect, it } from 'vitest';

import { groupedWhole, rowsShown } from './paging';

const rows = (count: number) => Array.from({ length: count }, (_, at) => at);

describe('a whole number with its thousands grouped', () => {
  it.each([
    [0, '0'],
    [7, '7'],
    [999, '999'],
    [1000, '1,000'],
    [12600, '12,600'],
    [100000, '100,000'],
    [1234567, '1,234,567'],
  ])('writes %i as %s', (count, text) => {
    expect(groupedWhole(count)).toBe(text);
  });
});

describe('where a page of trades sits', () => {
  it('reads from 1 on the first page, as the server counts it from offset 0, out of the server’s own total', () => {
    expect(rowsShown({ offset: 0, total: 12600, trades: rows(100) })).toBe('Trades 1 to 100 of 12,600');
  });

  it('reads the next page from the offset the server gave', () => {
    expect(rowsShown({ offset: 100, total: 12600, trades: rows(100) })).toBe('Trades 101 to 200 of 12,600');
  });

  it('ends at the last row it holds, which is not a full page on the last page', () => {
    expect(rowsShown({ offset: 200, total: 237, trades: rows(37) })).toBe('Trades 201 to 237 of 237');
  });

  it('groups the thousands at both ends', () => {
    expect(rowsShown({ offset: 1000, total: 12600, trades: rows(1000) })).toBe('Trades 1,001 to 2,000 of 12,600');
  });

  it('says so when a page past the end holds nothing, with the total the server counted', () => {
    expect(rowsShown({ offset: 500, total: 50, trades: [] })).toBe('No trades on this page, 50 in all');
  });

  it('says nothing at all when there are no trades to place', () => {
    expect(rowsShown({ offset: 0, total: 0, trades: [] })).toBe('');
  });
});
