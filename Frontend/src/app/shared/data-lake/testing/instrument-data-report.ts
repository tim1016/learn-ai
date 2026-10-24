import type { InstrumentDataReport } from '../data-lake.service';

/*
 * `GET /api/data-lake/instrument-data` as a spec needs it, built one way: from the sessions the spec names, with the
 * summary counted from them the way Python counts it. Pure helpers only: it imports no test runner.
 */

type Session = InstrumentDataReport['sessions'][number];

const DAY_MS = 86_400_000;

/** Session `n` of a window that opens on 2026-04-15's session. */
export function dataSession(n: number, state: Session['state'], extra: Partial<Session> = {}): Session {
  return { trading_date_ms: Date.UTC(2026, 3, 15, 13, 30) + n * DAY_MS, state, ...extra };
}

/**
 * A reading as Python sends it. The summary is counted from the sessions the
 * caller names, the way Python counts it, so a spec states sessions and reads
 * numbers off them.
 */
export function dataReport(
  sessions: readonly Session[],
  overrides: Partial<InstrumentDataReport> = {},
): InstrumentDataReport {
  const count = (state: Session['state']) => sessions.filter((s) => s.state === state).length;
  const unfinished = count('forming') + count('upcoming');
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
      calendar_day_count: sessions.length,
      session_count: sessions.length,
      ready: count('ready'),
      building: count('building'),
      needs_rebuild: count('needs_rebuild'),
      missing: count('missing'),
      failed: count('failed'),
      forming: count('forming'),
      upcoming: count('upcoming'),
      fetchable: sessions.filter((s) => s.fetchable).length,
      before_provider_history: sessions.filter((s) => s.blocked_by === 'before_provider_history').length,
      provider_no_data: sessions.filter((s) => s.blocked_by === 'provider_no_data').length,
      run_readable: sessions.length > unfinished && count('ready') === sessions.length - unfinished,
    },
    sessions: [...sessions],
    fill_window: null,
    daily_rollup: { state: 'current' },
    ...overrides,
  };
}

/** The sessions of a window by the state they are in; every state a spec leaves out is none, and the rest are held. */
export interface HeldSessions {
  readonly sessions: number;
  /** Finished sessions the lake lacks and a capture can fetch. */
  readonly missing?: number;
  /** Finished sessions the lake lacks that the provider no longer serves. */
  readonly beforeProviderHistory?: number;
  /** Finished sessions the provider answered with no bars. */
  readonly providerNoData?: number;
  /** Finished sessions a writer holds a claim on now. */
  readonly building?: number;
  readonly forming?: number;
}

/**
 * A reading of a window of `held.sessions` sessions, as Python would send it, with the span its capture would ask
 * for (first through last fetchable session) named, as Python names it.
 */
export function heldReport(held: HeldSessions): InstrumentDataReport {
  const { sessions, missing = 0, beforeProviderHistory = 0, providerNoData = 0, building = 0, forming = 0 } = held;
  const states: Session[] = [];
  const add = (count: number, make: (n: number) => Session) => {
    for (let i = 0; i < count; i += 1) states.push(make(states.length));
  };
  add(missing, (n) => dataSession(n, 'missing', { fetchable: true }));
  add(beforeProviderHistory, (n) => dataSession(n, 'missing', { fetchable: false, blocked_by: 'before_provider_history' }));
  add(providerNoData, (n) => dataSession(n, 'failed', { fetchable: false, reason: 'provider_no_data', blocked_by: 'provider_no_data' }));
  add(building, (n) => dataSession(n, 'building'));
  add(forming, (n) => dataSession(n, 'forming'));
  add(sessions - states.length, (n) => dataSession(n, 'ready'));
  const fetchable = states.filter((s) => s.fetchable);
  return dataReport(states, {
    fill_window:
      fetchable.length === 0
        ? null
        : { start_trading_date_ms: fetchable[0].trading_date_ms, end_trading_date_ms: fetchable[fetchable.length - 1].trading_date_ms },
  });
}
