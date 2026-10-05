import type { DataRunSpec } from './data-lake.types';

/**
 * One symbol's trade bars over a window of trading dates, in one lake tree.
 * Both instants are calendar-anchored, as `DataRunSpec`'s own bounds are.
 */
export interface TradeBackfillWindow {
  readonly symbol: string;
  readonly price_adjustment_mode: 'raw' | 'polygon_split_adjusted';
  readonly start_trading_date_ms: number;
  readonly end_trading_date_ms: number;
}

/**
 * The backfill job's spec for one symbol's trade bars over `window`. The
 * picker's coverage gate and a refused plan's remedy both submit this one.
 * `digest` is the data plane's pinned LEAN image, from its backfill defaults.
 */
export function tradeBackfillSpec(window: TradeBackfillWindow, digest: string): DataRunSpec {
  return {
    request_id: globalThis.crypto.randomUUID(),
    run_type: 'python_lab',
    market: 'usa',
    symbols: [window.symbol],
    start_trading_date_ms: window.start_trading_date_ms,
    end_trading_date_ms: window.end_trading_date_ms,
    data_types: ['trade'],
    lean_image_digest: digest,
    price_adjustment_mode: window.price_adjustment_mode,
  };
}
