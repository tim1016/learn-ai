import { describe, expect, it } from 'vitest';

import { buildCoverageBoard, coverageGlyph, parseSymbols } from './coverage-board';
import type { CoverageResponse, CoverageStatus, DataLakeRead } from './data-lake.types';

/** 09:30 America/New_York on the given 2026 date, as int64 ms UTC (EDT, UTC-4). */
function sessionOpenMs(day: number): number {
  return Date.UTC(2026, 4, day, 13, 30);
}

function coverage(
  symbol: string,
  days: readonly { day: number; status: CoverageStatus; artifactId?: number }[],
): DataLakeRead<CoverageResponse> {
  return {
    kind: 'ok',
    value: {
      market: 'usa',
      symbol,
      data_type: 'trade',
      resolution: 'minute',
      provider: 'polygon',
      price_adjustment_mode: 'raw',
      days: days.map((entry) => ({
        trading_date_ms: sessionOpenMs(entry.day),
        status: entry.status,
        artifact_id: entry.artifactId ?? null,
      })),
    },
  };
}

describe('buildCoverageBoard', () => {
  it('turns each symbol response into a row and tallies its states', () => {
    const board = buildCoverageBoard([
      {
        symbol: 'SPY',
        read: coverage('SPY', [
          { day: 18, status: 'complete', artifactId: 1 },
          { day: 19, status: 'failed', artifactId: 2 },
          { day: 20, status: 'missing' },
        ]),
      },
    ]);

    expect(board.rows).toHaveLength(1);
    expect(board.rows[0].counts).toEqual({
      complete: 1,
      fetching: 0,
      stale: 0,
      failed: 1,
      missing: 1,
    });
    expect(board.sessionCount).toBe(3);
    expect(board.firstSessionMs).toBe(sessionOpenMs(18));
    expect(board.lastSessionMs).toBe(sessionOpenMs(20));
  });

  it('carries only the sessions the calendar returned, so a weekend is never a cell', () => {
    // 2026-05-16/17 is a weekend; the endpoint walks sessions, so those
    // dates are simply absent from `days` and must not be invented here.
    const board = buildCoverageBoard([
      {
        symbol: 'SPY',
        read: coverage('SPY', [
          { day: 15, status: 'complete', artifactId: 1 },
          { day: 18, status: 'complete', artifactId: 2 },
        ]),
      },
    ]);

    expect(board.rows[0].cells.map((cell) => cell.tradingDateMs)).toEqual([
      sessionOpenMs(15),
      sessionOpenMs(18),
    ]);
  });

  it('names a rejected symbol as a problem instead of dropping it silently', () => {
    const board = buildCoverageBoard([
      { symbol: 'SPY', read: coverage('SPY', [{ day: 18, status: 'complete', artifactId: 1 }]) },
      {
        symbol: 'NOPE',
        read: { kind: 'rejected', reason: 'invalid_symbol', message: 'symbol must match ^[A-Z]' },
      },
    ]);

    expect(board.rows.map((row) => row.symbol)).toEqual(['SPY']);
    expect(board.problems).toEqual([
      { symbol: 'NOPE', reason: 'invalid_symbol', message: 'symbol must match ^[A-Z]' },
    ]);
  });

  it('reports a transport failure under its own reason code', () => {
    const board = buildCoverageBoard([
      { symbol: 'SPY', read: { kind: 'unavailable', message: 'The data plane did not respond.' } },
    ]);

    expect(board.problems[0].reason).toBe('unavailable');
  });

  it('flags the whole board dark when a read comes back not enabled', () => {
    const board = buildCoverageBoard([{ symbol: 'SPY', read: { kind: 'not_enabled' } }]);

    expect(board.notEnabled).toBe(true);
    expect(board.rows).toEqual([]);
  });

  it('takes the session axis from the longest row so a failed symbol cannot shorten it', () => {
    const board = buildCoverageBoard([
      { symbol: 'AAA', read: coverage('AAA', [{ day: 18, status: 'complete', artifactId: 1 }]) },
      {
        symbol: 'BBB',
        read: coverage('BBB', [
          { day: 18, status: 'complete', artifactId: 2 },
          { day: 19, status: 'complete', artifactId: 3 },
        ]),
      },
    ]);

    expect(board.sessionCount).toBe(2);
  });
});

describe('coverageGlyph', () => {
  it('gives every state its own glyph, so colour is never the only channel', () => {
    const glyphs = (['complete', 'fetching', 'stale', 'failed', 'missing'] as const).map(
      coverageGlyph,
    );

    expect(new Set(glyphs).size).toBe(glyphs.length);
  });
});

describe('parseSymbols', () => {
  it('upper-cases, splits on commas and spaces, and de-duplicates', () => {
    expect(parseSymbols('spy, aapl  SPY', 20).symbols).toEqual(['SPY', 'AAPL']);
  });

  it('partitions entries the catalog could never store', () => {
    const parsed = parseSymbols('SPY, 9X, TOOOOOOOOOOOOOOOOOOOOLONG', 20);

    expect(parsed.symbols).toEqual(['SPY']);
    expect(parsed.invalid).toEqual(['9X', 'TOOOOOOOOOOOOOOOOOOOOLONG']);
  });

  it('returns nothing for an empty entry rather than a blank symbol', () => {
    expect(parseSymbols('  ,  ', 20)).toEqual({ symbols: [], invalid: [] });
  });
});
