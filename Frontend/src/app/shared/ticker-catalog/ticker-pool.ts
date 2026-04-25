import type { TickerOption } from '../ticker-range-picker';

/**
 * Curated starter universe shared by pages that render the ticker picker.
 * Ordering is "most useful first" for dark-pool / ETF / mega-cap research.
 * Tickers outside this pool can still be used by editing the target
 * strategy's ``symbol`` field directly — the picker just doesn't surface them.
 */
export const TICKER_POOL: readonly TickerOption[] = [
  { symbol: 'SPY', name: 'SPDR S&P 500 ETF Trust', exchange: 'ARCA' },
  { symbol: 'QQQ', name: 'Invesco QQQ Trust', exchange: 'NASDAQ' },
  { symbol: 'IWM', name: 'iShares Russell 2000 ETF', exchange: 'ARCA' },
  { symbol: 'AAPL', name: 'Apple Inc.', exchange: 'NASDAQ' },
  { symbol: 'MSFT', name: 'Microsoft Corporation', exchange: 'NASDAQ' },
  { symbol: 'NVDA', name: 'NVIDIA Corporation', exchange: 'NASDAQ' },
  { symbol: 'TSLA', name: 'Tesla, Inc.', exchange: 'NASDAQ' },
  { symbol: 'AMZN', name: 'Amazon.com, Inc.', exchange: 'NASDAQ' },
  { symbol: 'META', name: 'Meta Platforms, Inc.', exchange: 'NASDAQ' },
  { symbol: 'GOOGL', name: 'Alphabet Inc.', exchange: 'NASDAQ' },
  { symbol: 'AMD', name: 'Advanced Micro Devices', exchange: 'NASDAQ' },
];

export const RECENT_TICKERS: readonly string[] = ['SPY', 'QQQ', 'AAPL'];
