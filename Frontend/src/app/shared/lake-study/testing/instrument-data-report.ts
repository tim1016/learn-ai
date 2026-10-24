import type { InstrumentDataReport } from '../../data-lake';

/** The sessions of a span as Python counts them; every count a test leaves out is zero. */
export interface HeldSessions {
  readonly sessions: number;
  /** Finished sessions the lake lacks and a capture can fetch. */
  readonly missing?: number;
  /** Finished sessions the lake lacks that the provider no longer serves. */
  readonly beforeProviderHistory?: number;
  /** Finished sessions a writer holds a claim on now. */
  readonly building?: number;
  readonly forming?: number;
}

const FIRST_FETCHABLE_MS = Date.UTC(2024, 2, 4, 12);
const LAST_FETCHABLE_MS = Date.UTC(2024, 2, 15, 12);

/** `GET /api/data-lake/instrument-data` for a span, with just the counts and fill window a lake study's page reads. */
export function instrumentDataReport(held: HeldSessions): InstrumentDataReport {
  const { sessions, missing = 0, beforeProviderHistory = 0, building = 0, forming = 0 } = held;
  const fetchable = missing;
  return {
    market: 'usa',
    symbol: 'SPY',
    data_type: 'trade',
    resolution: 'minute',
    price_adjustment_mode: 'polygon_split_adjusted',
    data_root_id: '00000000-0000-0000-0000-000000000000',
    provider_history_start_ms: Date.UTC(2021, 9, 26, 12),
    as_of_ms: Date.UTC(2026, 9, 24, 12),
    summary: {
      calendar_day_count: sessions,
      session_count: sessions,
      ready: sessions - missing - beforeProviderHistory - building - forming,
      building,
      needs_rebuild: 0,
      missing: missing + beforeProviderHistory,
      failed: 0,
      forming,
      upcoming: 0,
      fetchable,
      before_provider_history: beforeProviderHistory,
      provider_no_data: 0,
      run_readable: false,
    },
    sessions: [],
    fill_window: fetchable > 0 ? { start_trading_date_ms: FIRST_FETCHABLE_MS, end_trading_date_ms: LAST_FETCHABLE_MS } : null,
    daily_rollup: { state: 'current' },
  };
}

export { FIRST_FETCHABLE_MS, LAST_FETCHABLE_MS };
