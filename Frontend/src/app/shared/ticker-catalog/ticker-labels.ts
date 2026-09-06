/**
 * Display labels for instruments — names and listing venues, nothing else.
 *
 * This map deliberately does **not** decide which instruments the picker
 * offers. Membership comes from the data lake (see `TickerCatalogService`),
 * which is the sole market-data store since #1893: an instrument is
 * selectable exactly when the lake holds bars for it. This file only answers
 * "what do we call it", and a symbol missing from it still appears — labelled
 * by its own symbol.
 *
 * Before this split the picker's universe *was* a hardcoded list, and the two
 * had drifted apart in both directions: it offered QQQ, AMZN, META, GOOGL and
 * AMD, for which the lake holds nothing, while GLD, SLV, DIA, GE and STRL —
 * all fully backfilled — were unreachable by any UI path.
 */
export interface TickerLabel {
  readonly name: string;
  readonly exchange: string;
}

export const TICKER_LABELS: Readonly<Record<string, TickerLabel>> = {
  SPY: { name: 'SPDR S&P 500 ETF Trust', exchange: 'ARCA' },
  QQQ: { name: 'Invesco QQQ Trust', exchange: 'NASDAQ' },
  IWM: { name: 'iShares Russell 2000 ETF', exchange: 'ARCA' },
  DIA: { name: 'SPDR Dow Jones Industrial Average ETF', exchange: 'ARCA' },
  GLD: { name: 'SPDR Gold Shares', exchange: 'ARCA' },
  SLV: { name: 'iShares Silver Trust', exchange: 'ARCA' },
  AAPL: { name: 'Apple Inc.', exchange: 'NASDAQ' },
  MSFT: { name: 'Microsoft Corporation', exchange: 'NASDAQ' },
  NVDA: { name: 'NVIDIA Corporation', exchange: 'NASDAQ' },
  TSLA: { name: 'Tesla, Inc.', exchange: 'NASDAQ' },
  AMZN: { name: 'Amazon.com, Inc.', exchange: 'NASDAQ' },
  META: { name: 'Meta Platforms, Inc.', exchange: 'NASDAQ' },
  GOOGL: { name: 'Alphabet Inc.', exchange: 'NASDAQ' },
  AMD: { name: 'Advanced Micro Devices', exchange: 'NASDAQ' },
  GE: { name: 'GE Aerospace', exchange: 'NYSE' },
  STRL: { name: 'Sterling Infrastructure, Inc.', exchange: 'NASDAQ' },
};

/**
 * Symbols to surface under "Recent" when the operator has picked nothing yet.
 *
 * A seed, not a guarantee: `TickerCatalogService` intersects it with what the
 * lake actually holds, so a symbol listed here that has no bars never reaches
 * the dropdown. QQQ used to head this list and was silently dropped by that
 * intersection — the lake holds nothing for it.
 */
export const SEED_RECENT_TICKERS: readonly string[] = ['SPY', 'AAPL', 'GLD'];
