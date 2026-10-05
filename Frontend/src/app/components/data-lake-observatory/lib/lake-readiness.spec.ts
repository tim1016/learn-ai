import { describe, expect, it } from 'vitest';

import type { SymbolRunReadiness } from '../../../shared/data-lake';
import {
  coverageWindow,
  fillRows,
  readinessRows,
  readinessTotals,
  rebuildRequests,
} from './lake-readiness';

/** 09:30 America/New_York on a date, as int64 ms UTC (EDT dates only). */
function sessionOpenMs(year: number, month: number, day: number): number {
  return Date.UTC(year, month - 1, day, 13, 30);
}

function entry(overrides: Partial<SymbolRunReadiness> & { symbol: string }): SymbolRunReadiness {
  return {
    first_trading_date_ms: sessionOpenMs(2024, 8, 6),
    last_trading_date_ms: sessionOpenMs(2026, 9, 18),
    session_count: 532,
    run_ready_count: 532,
    unrebuildable_count: 0,
    rebuild_first_trading_date_ms: null,
    rebuild_last_trading_date_ms: null,
    ...overrides,
  };
}

const GLD = entry({
  symbol: 'GLD',
  run_ready_count: 0,
  rebuild_first_trading_date_ms: sessionOpenMs(2024, 8, 6),
  rebuild_last_trading_date_ms: sessionOpenMs(2026, 9, 18),
});
const SPY = entry({ symbol: 'SPY', session_count: 1252, run_ready_count: 1252 });
/** Nine sessions older than the provider's history; the rest can be rebuilt. */
const AMZN = entry({
  symbol: 'AMZN',
  session_count: 1254,
  run_ready_count: 0,
  unrebuildable_count: 9,
  rebuild_first_trading_date_ms: sessionOpenMs(2021, 10, 5),
  rebuild_last_trading_date_ms: sessionOpenMs(2026, 9, 18),
});
/** Rebuilt already: only the sessions the provider no longer serves stay unreadable. */
const META = entry({ symbol: 'META', session_count: 71, run_ready_count: 63, unrebuildable_count: 8 });

describe('readinessRows', () => {
  it('puts symbols a backfill can fix first, then the rest that are unreadable, then ready', () => {
    const rows = readinessRows([SPY, META, GLD, AMZN], 'trade');

    expect(rows.map((row) => [row.symbol, row.state])).toEqual([
      ['AMZN', 'unreadable'],
      ['GLD', 'unreadable'],
      ['META', 'partial'],
      ['SPY', 'ready'],
    ]);
  });

  it('separates the sessions a backfill can rebuild from the ones too old to fetch again', () => {
    const [amzn] = readinessRows([AMZN], 'trade');

    expect(amzn.rebuildableCount).toBe(1245);
    expect(amzn.unrebuildableCount).toBe(9);
    // The window starts at the oldest session the provider still serves.
    expect(amzn.rebuild).toEqual({
      symbol: 'AMZN',
      from: '2021-10-05',
      to: '2026-09-18',
      includeQuotes: false,
    });
  });

  it('asks a rebuild of quote bars to derive quotes, since a backfill writes trade bars alone otherwise', () => {
    const [gld] = readinessRows([GLD], 'quote');

    expect(gld.rebuild?.includeQuotes).toBe(true);
  });

  it('offers no rebuild for a symbol whose only unreadable sessions are too old', () => {
    const [meta] = readinessRows([META], 'trade');

    expect(meta.rebuildableCount).toBe(0);
    expect(meta.rebuild).toBeNull();
  });
});

/** Backfilled twice: nine sessions in October 2021, then a run from 2024-09-04, and nothing between. */
const QQQ = entry({
  symbol: 'QQQ',
  first_trading_date_ms: sessionOpenMs(2021, 10, 5),
  session_count: 531,
  run_ready_count: 531,
  gap_session_count: 724,
  gap_count: 1,
  unfillable_gap_session_count: 10,
  fill_first_trading_date_ms: sessionOpenMs(2021, 11, 1),
  fill_last_trading_date_ms: sessionOpenMs(2024, 9, 3),
});

describe("gaps between a symbol's first and last session", () => {
  it('asks a backfill for the missing sessions the provider still serves', () => {
    const [qqq] = readinessRows([QQQ], 'trade');

    expect([qqq.state, qqq.gapSessionCount, qqq.gapCount, qqq.unfillableGapCount]).toEqual(['ready', 724, 1, 10]);
    expect(qqq.fill).toEqual({ symbol: 'QQQ', from: '2021-11-01', to: '2024-09-03', includeQuotes: false });
    expect(qqq.rebuild).toBeNull();
  });

  it('puts a gap to fill after the rebuilds and ahead of everything a backfill cannot help', () => {
    const rows = readinessRows([SPY, META, QQQ, GLD], 'trade');

    expect(rows.map((row) => row.symbol)).toEqual(['GLD', 'QQQ', 'META', 'SPY']);
    expect(fillRows(rows).map((row) => row.fill.symbol)).toEqual(['QQQ']);
  });

  it('reads no gap from a data plane that does not report them', () => {
    const [spy] = readinessRows([SPY], 'trade');

    expect([spy.gapSessionCount, spy.gapCount, spy.unfillableGapCount, spy.fill]).toEqual([0, 0, 0, null]);
  });
});

describe('readinessTotals', () => {
  it('counts only the symbols and sessions a backfill can rebuild', () => {
    const rows = readinessRows([SPY, META, GLD, AMZN], 'trade');

    expect(readinessTotals(rows)).toEqual({
      symbolCount: 4,
      readyCount: 1,
      rebuildSymbolCount: 2,
      rebuildSessionCount: 532 + 1245,
      unrebuildableSessionCount: 17,
    });
    expect(rebuildRequests(rows).map((request) => request.symbol)).toEqual(['AMZN', 'GLD']);
  });
});

describe('coverageWindow', () => {
  it('reads the whole held span when the coverage endpoint accepts it', () => {
    const [gld] = readinessRows([GLD], 'trade');

    expect(coverageWindow(gld, 1830)).toEqual({ from: '2024-08-06', to: '2026-09-18' });
  });

  it('keeps the newest sessions when the span is longer than the endpoint accepts', () => {
    const [gld] = readinessRows([GLD], 'trade');

    expect(coverageWindow(gld, 30)).toEqual({ from: '2026-08-20', to: '2026-09-18' });
  });
});
