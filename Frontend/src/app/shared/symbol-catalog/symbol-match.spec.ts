import { describe, it, expect } from 'vitest';

import { matchSymbols } from './symbol-match';
import type { PickerSymbol } from './symbol-catalog.types';

function row(symbol: string, name: string): PickerSymbol {
  return { symbol, name, delisted: false };
}

function symbols(rows: readonly PickerSymbol[]): string[] {
  return rows.map((r) => r.symbol);
}

describe('matchSymbols', () => {
  it('ranks the typed ticker, then tickers starting with it, then containing it, then name-only matches', () => {
    // TQQQ leads the pool the way a lake-held row does: pool order must
    // survive inside a tier, and only inside it.
    const pool = [
      row('TQQQ', 'ProShares UltraPro QQQ'),
      row('CQQQ', 'Invesco China Technology ETF'),
      row('PSQ', 'ProShares Short QQQ'),
      row('QQQ', 'Invesco QQQ Trust, Series 1'),
      row('QQQM', 'Invesco NASDAQ 100 ETF'),
      row('SPY', 'SPDR S&P 500 ETF'),
    ];

    expect(symbols(matchSymbols(pool, ' qqq '))).toEqual(['QQQ', 'QQQM', 'TQQQ', 'CQQQ', 'PSQ']);
  });

  it('finds a mixed-case ticker by its full symbol', () => {
    const pool = [
      row('ABR', 'Arbor Realty Trust'),
      row('ABRpD', 'Arbor Realty Trust 6.375% Series D Preferred'),
    ];

    expect(symbols(matchSymbols(pool, 'ABRpD'))).toEqual(['ABRpD']);
  });
});
