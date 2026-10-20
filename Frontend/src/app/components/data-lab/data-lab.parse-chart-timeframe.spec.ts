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
  it('maps chart timeframe labels to timespan and multiplier, and rejects unrecognized vocabulary', () => {
    expect.soft(parseChartTimeframe('1m'), 'parses minute timeframes: 1m').toEqual({ timespan: 'minute', multiplier: 1 });
    expect.soft(parseChartTimeframe('5m'), 'parses minute timeframes: 5m').toEqual({ timespan: 'minute', multiplier: 5 });
    expect.soft(parseChartTimeframe('15m'), 'parses minute timeframes: 15m').toEqual({ timespan: 'minute', multiplier: 15 });
    expect.soft(parseChartTimeframe('30m'), 'parses minute timeframes: 30m').toEqual({ timespan: 'minute', multiplier: 30 });

    expect.soft(parseChartTimeframe('1h'), 'parses hour timeframes: 1h').toEqual({ timespan: 'hour', multiplier: 1 });
    expect.soft(parseChartTimeframe('4h'), 'parses hour timeframes: 4h').toEqual({ timespan: 'hour', multiplier: 4 });

    expect.soft(parseChartTimeframe('1D'), 'parses day timeframes: 1D').toEqual({ timespan: 'day', multiplier: 1 });
    expect.soft(parseChartTimeframe('1W'), 'parses week timeframes: 1W').toEqual({ timespan: 'week', multiplier: 1 });
    expect.soft(parseChartTimeframe('1M'), 'parses month timeframes: 1M').toEqual({ timespan: 'month', multiplier: 1 });

    expect.soft(parseChartTimeframe(''), 'returns null for unrecognized vocabulary: empty string').toBeNull();
    expect.soft(parseChartTimeframe('1d'), "returns null for unrecognized vocabulary: '1d' (chart uses uppercase 'D')").toBeNull();
    expect.soft(parseChartTimeframe('15'), "returns null for unrecognized vocabulary: '15'").toBeNull();
    expect.soft(parseChartTimeframe('m15'), "returns null for unrecognized vocabulary: 'm15'").toBeNull();
    expect.soft(parseChartTimeframe('1y'), "returns null for unrecognized vocabulary: '1y'").toBeNull();
  });
});
