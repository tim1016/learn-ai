import { describe, expect, it } from 'vitest';

import {
  MAX_TRADING_RANGE_DAYS,
  tradingDateToMs,
  tradingRangeRejection,
  tradingRangeSpanDays,
} from './trading-range';

/**
 * The catalog's own cap, transcribed from
 * `PythonDataService/app/data_lake/types.py`:
 *
 *     _MAX_RANGE_YEARS = 5
 *     MAX_TRADING_RANGE_DAYS = _MAX_RANGE_YEARS * 366
 *
 * Pinned here so the client fallback cannot drift from the write-path
 * validator and the coverage endpoint that both enforce it.
 */
const BACKEND_MAX_RANGE_YEARS = 5;
const BACKEND_MAX_TRADING_RANGE_DAYS = BACKEND_MAX_RANGE_YEARS * 366;

describe('range cap parity with the catalog', () => {
  it('uses the backend constant', () => {
    expect(MAX_TRADING_RANGE_DAYS).toBe(BACKEND_MAX_TRADING_RANGE_DAYS);
    expect(MAX_TRADING_RANGE_DAYS).toBe(1830);
  });
});

describe('tradingRangeSpanDays', () => {
  it.each([
    ['2026-05-20', '2026-05-20', 1],
    ['2026-05-20', '2026-05-21', 2],
    // The backend formula is (end - start).days + 1 — inclusive of both ends.
    ['2026-05-01', '2026-05-31', 31],
    ['2026-01-01', '2026-12-31', 365],
    ['2024-01-01', '2024-12-31', 366],
  ])('spans %s → %s as %i days', (start, end, expected) => {
    expect(tradingRangeSpanDays(start, end)).toBe(expected);
  });

  it('counts calendar days across a DST boundary without losing an hour', () => {
    // 2026-03-08 is the US spring-forward. A local-midnight subtraction
    // would come out at 23 hours and round to the wrong day count.
    expect(tradingRangeSpanDays('2026-03-07', '2026-03-09')).toBe(3);
    expect(tradingRangeSpanDays('2026-10-31', '2026-11-02')).toBe(3);
  });

  it('reports an inverted window as a non-positive span', () => {
    expect(tradingRangeSpanDays('2026-05-21', '2026-05-20')).toBe(0);
  });

  it.each(['', '2026-5-20', '20260520', 'yesterday'])(
    'refuses to guess a span for %s',
    (value) => {
      expect(tradingRangeSpanDays(value, '2026-05-20')).toBeNull();
      expect(tradingRangeSpanDays('2026-05-20', value)).toBeNull();
    },
  );
});

describe('tradingRangeRejection', () => {
  it('accepts a window exactly at the cap', () => {
    // 1830 inclusive days starting 2026-01-01.
    const end = new Date(Date.UTC(2026, 0, 1) + (BACKEND_MAX_TRADING_RANGE_DAYS - 1) * 86_400_000)
      .toISOString()
      .slice(0, 10);

    expect(tradingRangeSpanDays('2026-01-01', end)).toBe(BACKEND_MAX_TRADING_RANGE_DAYS);
    expect(tradingRangeRejection('2026-01-01', end, BACKEND_MAX_TRADING_RANGE_DAYS)).toBeNull();
  });

  it('blocks a window one day past the cap, naming the cap', () => {
    const end = new Date(Date.UTC(2026, 0, 1) + BACKEND_MAX_TRADING_RANGE_DAYS * 86_400_000)
      .toISOString()
      .slice(0, 10);

    expect(tradingRangeRejection('2026-01-01', end, BACKEND_MAX_TRADING_RANGE_DAYS)).toBe(
      'That window is 1831 days; the data plane accepts at most 1830.',
    );
  });

  it('names an inverted window before it names the cap', () => {
    expect(tradingRangeRejection('2026-05-21', '2026-05-20', 1830)).toBe(
      'The start date is after the end date.',
    );
  });

  it.each([
    ['', '2026-05-20'],
    ['2026-05-20', ''],
    ['not-a-date', '2026-05-20'],
  ])('asks for a date range when given (%s, %s)', (start, end) => {
    expect(tradingRangeRejection(start, end, 1830)).toBe('Pick a date range.');
  });

  it('honours a cap the data plane lowered', () => {
    expect(tradingRangeRejection('2026-05-01', '2026-05-31', 30)).toBe(
      'That window is 31 days; the data plane accepts at most 30.',
    );
    expect(tradingRangeRejection('2026-05-01', '2026-05-30', 30)).toBeNull();
  });
});

describe('tradingDateToMs', () => {
  it('resolves back to the same calendar date in Eastern Time', () => {
    // Both DST states, because the anchor exists to survive the offset
    // flipping: 2026-01-15 is EST (UTC-5), 2026-07-15 is EDT (UTC-4).
    for (const iso of ['2026-01-15', '2026-07-15']) {
      const ms = tradingDateToMs(iso);
      expect(ms).not.toBeNull();
      const inEt = new Intl.DateTimeFormat('en-CA', {
        timeZone: 'America/New_York',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
      }).format(new Date(ms as number));
      expect(inEt).toBe(iso);
    }
  });

  it('does not use UTC midnight, which would shift the date back a day', () => {
    // The bug this anchor exists to avoid: midnight UTC on 2026-01-15 is
    // 19:00 ET on 2026-01-14, so every window would silently start a day early.
    const ms = tradingDateToMs('2026-01-15') as number;
    expect(ms).toBeGreaterThan(Date.UTC(2026, 0, 15));
  });

  it('returns null for anything that is not a YYYY-MM-DD date', () => {
    expect(tradingDateToMs('')).toBeNull();
    expect(tradingDateToMs('not-a-date')).toBeNull();
    expect(tradingDateToMs('2026-5-1')).toBeNull();
  });
});
