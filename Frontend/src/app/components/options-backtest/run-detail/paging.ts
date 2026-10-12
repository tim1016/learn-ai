import type { TradesPage } from './run-detail.types';

/** How many trades one page asks for. The server's own default is ten times this; a table of that many rows is hard to read. */
export const TRADES_PER_PAGE = 100;

const THOUSANDS = /\B(?=(\d{3})+(?!\d))/g;

/** A whole number with a comma between its thousands: 12600 reads 12,600. Digits only, as text, so a count never passes through a pipe. */
export function groupedWhole(count: number): string {
  return String(count).replace(THOUSANDS, ',');
}

/**
 * Where a page of trades sits among all of them, in words: "Trades 1 to 100 of 12,600". The ends are the served `offset`
 * and the number of rows the page holds, counted from 1 as a reader counts; `total` is the server's own. This is the one
 * place the page adds to a number the server sent, and what it adds to is a position in a list, never a price or a total.
 * A page with no rows says so (a page past the end, which a filter can leave) and nothing at all when there are no trades.
 */
export function rowsShown(page: Pick<TradesPage, 'offset' | 'total'> & { trades: readonly unknown[] }): string {
  if (page.trades.length === 0) return page.total === 0 ? '' : `No trades on this page, ${groupedWhole(page.total)} in all`;
  const first = page.offset + 1;
  const last = page.offset + page.trades.length;
  return `Trades ${groupedWhole(first)} to ${groupedWhole(last)} of ${groupedWhole(page.total)}`;
}
