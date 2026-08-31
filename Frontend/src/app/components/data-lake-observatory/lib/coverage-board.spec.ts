import { describe, expect, it } from 'vitest';

import { SYMBOL_PATTERN, buildCoverageBoard, coverageGlyph, parseSymbols } from './coverage-board';
import type { CoverageResponse, CoverageStatus, DataLakeRead } from './data-lake.types';

/**
 * `SYMBOL_RE` as `PythonDataService/app/data_lake/types.py` declares it.
 *
 * Transcribed once, here, so the client copy of the grammar is pinned to
 * the catalog's own. Change one side and this fails; that is the point —
 * a silently diverged copy would start rejecting symbols the catalog
 * accepts, or waving through ones the `character varying(20)` column and
 * the write-path validator would refuse.
 */
const BACKEND_SYMBOL_RE_PATTERN = '^[A-Z][A-Z0-9.]*$';
/** `MAX_SYMBOL_LENGTH` from the same module — the catalog's column width. */
const BACKEND_MAX_SYMBOL_LENGTH = 20;

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

describe('symbol grammar parity with the catalog', () => {
  it('uses the backend pattern character for character', () => {
    expect(SYMBOL_PATTERN.source).toBe(BACKEND_SYMBOL_RE_PATTERN);
    expect(SYMBOL_PATTERN.flags).toBe('');
  });

  it.each([
    ['SPY', true],
    ['BRK.B', true],
    ['A', true],
    ['X1', true],
    ['spy', false],
    ['9X', false],
    ['.SPY', false],
    ['SP-Y', false],
    ['', false],
  ])('accepts %s exactly as the catalog does: %s', (symbol, storable) => {
    expect(SYMBOL_PATTERN.test(symbol)).toBe(storable);
  });

  it('validates only after upper-casing, so an operator may type lower case', () => {
    // The pattern itself rejects `spy`; `parseSymbols` canonicalises first,
    // which is why a lower-case entry is accepted and sent as `SPY`.
    expect(SYMBOL_PATTERN.test('spy')).toBe(false);
    expect(parseSymbols('spy', BACKEND_MAX_SYMBOL_LENGTH)).toEqual({
      symbols: ['SPY'],
      invalid: [],
    });
  });

  it.each(['9X', '.SPY', 'SP-Y'])('refuses %s, which the catalog could not store', (symbol) => {
    expect(parseSymbols(symbol, BACKEND_MAX_SYMBOL_LENGTH)).toEqual({
      symbols: [],
      invalid: [symbol],
    });
  });

  it('rejects a symbol one character past the catalog column width', () => {
    const atCap = 'A'.repeat(BACKEND_MAX_SYMBOL_LENGTH);
    const overCap = 'A'.repeat(BACKEND_MAX_SYMBOL_LENGTH + 1);

    expect(parseSymbols(atCap, BACKEND_MAX_SYMBOL_LENGTH).symbols).toEqual([atCap]);
    expect(parseSymbols(overCap, BACKEND_MAX_SYMBOL_LENGTH).invalid).toEqual([overCap]);
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
