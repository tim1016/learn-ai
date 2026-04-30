/**
 * Regression: chart timeframe selector → ZIP download payload.
 *
 * Bug 2026-04-29: picking 15m on the chart's timeframe bar left the
 * parent's (timespan, multiplier) at the auto-derived defaults, so the
 * downloaded dataset.csv contained 1-minute bars even though the chart
 * preview showed 15-minute aggregates. The parser below feeds
 * onChartTimeframeChanged, which now syncs the chart's pick into the
 * ZIP payload.
 */
import { describe, it, expect } from 'vitest';
import { parseChartTimeframe } from './data-lab.component';

describe('parseChartTimeframe', () => {
  it('parses minute timeframes', () => {
    expect(parseChartTimeframe('1m')).toEqual({ timespan: 'minute', multiplier: 1 });
    expect(parseChartTimeframe('5m')).toEqual({ timespan: 'minute', multiplier: 5 });
    expect(parseChartTimeframe('15m')).toEqual({ timespan: 'minute', multiplier: 15 });
    expect(parseChartTimeframe('30m')).toEqual({ timespan: 'minute', multiplier: 30 });
  });

  it('parses hour timeframes', () => {
    expect(parseChartTimeframe('1h')).toEqual({ timespan: 'hour', multiplier: 1 });
    expect(parseChartTimeframe('4h')).toEqual({ timespan: 'hour', multiplier: 4 });
  });

  it('parses day, week, month timeframes', () => {
    expect(parseChartTimeframe('1D')).toEqual({ timespan: 'day', multiplier: 1 });
    expect(parseChartTimeframe('1W')).toEqual({ timespan: 'week', multiplier: 1 });
    expect(parseChartTimeframe('1M')).toEqual({ timespan: 'month', multiplier: 1 });
  });

  it('returns null for unrecognized vocabulary', () => {
    expect(parseChartTimeframe('')).toBeNull();
    expect(parseChartTimeframe('1d')).toBeNull(); // chart uses uppercase 'D'
    expect(parseChartTimeframe('15')).toBeNull();
    expect(parseChartTimeframe('m15')).toBeNull();
    expect(parseChartTimeframe('1y')).toBeNull();
  });
});
