import type { PickerSymbol } from './symbol-catalog.types';

/**
 * The picker rows a typed query matches, best match first.
 *
 * Membership is a substring of the ticker or the name. Order is the
 * operator's intent: the ticker they typed, then tickers that start with
 * it, then tickers that contain it, then name-only matches — pool order
 * (held-first) kept inside each tier. Every picker caps the rows it
 * renders, so an unranked list hides a typed symbol behind whatever sorts
 * ahead of it: sixteen listed rows match "QQQ" before QQQ itself.
 *
 * Tickers compare case-insensitively — preferred shares list as `ABRpD`.
 */
export function matchSymbols(
  pool: readonly PickerSymbol[],
  typed: string,
): readonly PickerSymbol[] {
  const query = typed.trim().toUpperCase();
  if (!query) return pool;
  const tiers: PickerSymbol[][] = [[], [], [], []];
  for (const row of pool) {
    const tier = matchTier(row, query);
    if (tier !== null) tiers[tier].push(row);
  }
  return tiers.flat();
}

function matchTier(row: PickerSymbol, query: string): number | null {
  const symbol = row.symbol.toUpperCase();
  if (symbol === query) return 0;
  if (symbol.startsWith(query)) return 1;
  if (symbol.includes(query)) return 2;
  return row.name.toUpperCase().includes(query) ? 3 : null;
}
