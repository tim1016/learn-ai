import { describe, expect, it } from 'vitest';

import {
  buildChartRequestBody,
  buildDatasetPlanPayload,
  buildGenerateZipPayload,
  utcDayEndMs,
  utcMsToIsoDate,
} from './data-lab-request-mapper';
import { DataLabIndicatorInstance } from './data-lab-workspace-store';

// 2026-01-02T00:00:00Z → 2026-06-30T00:00:00Z
const WINDOW = { startMsUtc: Date.UTC(2026, 0, 2), endMsUtc: Date.UTC(2026, 5, 30) };

const INDICATORS: DataLabIndicatorInstance[] = [
  { id: 'ema|length=10', canonicalKey: 'ema', params: { length: 10 } },
  { id: 'rsi|length=14', canonicalKey: 'rsi', params: { length: 14 } },
];

describe('utcMsToIsoDate', () => {
  it('formats int64 ms UTC instants as UTC calendar dates', () => {
    expect(utcMsToIsoDate(Date.UTC(2026, 0, 2))).toBe('2026-01-02');
    expect(utcMsToIsoDate(Date.UTC(2026, 5, 30, 23, 59, 59))).toBe('2026-06-30');
  });
});

describe('utcDayEndMs', () => {
  it('anchors a TO date at its final UTC instant — same date, non-degenerate window', () => {
    const dayStart = Date.UTC(2026, 8, 11);
    const dayEnd = utcDayEndMs(dayStart);
    // Still the same UTC trading date for the chart's date-floor resolution…
    expect(utcMsToIsoDate(dayEnd)).toBe('2026-09-11');
    // …but strictly after the start, so half-open readers (news query,
    // dataset numeric overrides) never see an empty single-day window.
    expect(dayEnd).toBe(dayStart + 86_400_000 - 1);
    expect(dayEnd).toBeGreaterThan(dayStart);
  });
});

describe('buildChartRequestBody', () => {
  it('maps a representative workspace state to the /api/chart/data body', () => {
    const body = buildChartRequestBody({
      ticker: 'AAPL',
      window: WINDOW,
      timeframe: 'hour',
      session: 'regular',
      forwardFill: true,
      adjusted: true,
      indicators: INDICATORS,
      computeAllIndicators: false,
    });
    expect(body).toEqual({
      ticker: 'AAPL',
      from_date: '2026-01-02',
      to_date: '2026-06-30',
      start_ms_utc: WINDOW.startMsUtc,
      end_ms_utc: WINDOW.endMsUtc,
      timeframe: 'hour',
      // The store's default "Regular" session is `regular`, but Python only
      // recognizes `rth` — the mapper translates on the wire.
      session: 'rth',
      forward_fill: true,
      adjusted: true,
      indicators: [
        { name: 'ema', params: { length: 10 } },
        { name: 'rsi', params: { length: 14 } },
      ],
      compute_all_indicators: false,
    });
  });

  it('is pure — inputs are not mutated and repeat calls are stable', () => {
    const input = {
      ticker: 'MSFT',
      window: { ...WINDOW },
      timeframe: 'day',
      session: 'regular',
      forwardFill: false,
      adjusted: false,
      indicators: INDICATORS,
      computeAllIndicators: true,
    };
    const snapshot = JSON.stringify(input);
    const first = buildChartRequestBody(input);
    const second = buildChartRequestBody(input);
    expect(JSON.stringify(input)).toBe(snapshot);
    expect(first).toEqual(second);
    // emitted indicator params are copies, not shared references
    expect(first.indicators[0]?.params).not.toBe(INDICATORS[0].params);
  });
});

describe('buildGenerateZipPayload', () => {
  const OPTIONS = {
    enabled: true,
    strikes_each_side: 3,
    include_calls: true,
    include_puts: true,
    dte_distance: 5,
    include_ohlcv: true,
    include_vwap: true,
    include_transactions: true,
    include_open_interest: true,
    include_iv: true,
    include_delta: true,
    include_gamma: true,
    include_theta: true,
    include_vega: true,
    include_rho: true,
    include_discontinuity: true,
    risk_free_rate: 0.05,
    dividend_yield: 0,
  };

  it('maps a representative workspace state to the legacy generate-zip shape', () => {
    const payload = buildGenerateZipPayload({
      ticker: 'AAPL',
      window: WINDOW,
      indicators: INDICATORS,
      session: 'regular',
      forwardFill: true,
      adjusted: true,
      companions: {
        optionsCompanionEnabled: true,
        includeQualityReport: true,
        includePreviousClose: false,
        includeSplits: false,
        includeDividends: false,
        includeTickerOverview: true,
        includeNews: true,
        includeFinancials: false,
        includeStockTrades: false,
        includeStockQuotes: false,
      },
      options: OPTIONS,
      warmup: true,
      adjustForDividends: true,
      timespan: 'day',
      multiplier: 1,
      sort: 'timestamp',
      limit: 50000,
      timeZone: 'America/Chicago',
      columns: ['close', 'rsi_length14'],
    });

    expect(payload).toEqual({
      ticker: 'AAPL',
      from_date: '2026-01-02',
      to_date: '2026-06-30',
      start_ms_utc: WINDOW.startMsUtc,
      end_ms_utc: WINDOW.endMsUtc,
      indicator_entries: [
        { name: 'ema', params: { length: 10 } },
        { name: 'rsi', params: { length: 14 } },
      ],
      session: 'rth',
      forward_fill: true,
      fail_on_gaps: false,
      adjusted: true,
      adjust_for_dividends: true,
      warmup: true,
      timespan: 'day',
      multiplier: 1,
      sort: 'timestamp',
      limit: 50000,
      options_companion: OPTIONS,
      include_quality_report: true,
      include_previous_close: false,
      include_splits: false,
      include_dividends: false,
      include_ticker_overview: true,
      include_news: true,
      include_financials: false,
      include_trades: false,
      include_quotes: false,
      time_zone: 'America/Chicago',
      columns: ['close', 'rsi_length14'],
    });
  });

  it('builds the plan body as the same recipe without the column selection', () => {
    const input = {
      ticker: 'AAPL',
      window: WINDOW,
      indicators: INDICATORS,
      session: 'regular',
      forwardFill: false,
      adjusted: true,
      companions: {
        optionsCompanionEnabled: false,
        includeQualityReport: false,
        includePreviousClose: true,
        includeSplits: false,
        includeDividends: false,
        includeTickerOverview: false,
        includeNews: false,
        includeFinancials: false,
        includeStockTrades: false,
        includeStockQuotes: false,
      },
      options: OPTIONS,
      warmup: true,
      adjustForDividends: false,
      timespan: 'minute',
      multiplier: 5,
      sort: 'asc',
      limit: 50000,
      timeZone: null,
      columns: ['close'],
    };
    const plan = buildDatasetPlanPayload(input);
    const { columns, ...recipe } = buildGenerateZipPayload(input);

    expect(columns).toEqual(['close']);
    expect(plan).toEqual(recipe);
    expect('columns' in plan).toBe(false);
    expect(plan['time_zone']).toBeNull();
  });

  it('nulls options_companion and derives fail_on_gaps when forward-fill is off', () => {
    const payload = buildGenerateZipPayload({
      ticker: 'AAPL',
      window: WINDOW,
      indicators: [],
      session: 'regular',
      forwardFill: false,
      adjusted: true,
      companions: {
        optionsCompanionEnabled: false,
        includeQualityReport: false,
        includePreviousClose: false,
        includeSplits: false,
        includeDividends: false,
        includeTickerOverview: false,
        includeNews: false,
        includeFinancials: false,
        includeStockTrades: false,
        includeStockQuotes: false,
      },
      options: OPTIONS,
      warmup: true,
      adjustForDividends: false,
      timespan: 'day',
      multiplier: 1,
      sort: 'timestamp',
      limit: 50000,
      timeZone: null,
      columns: null,
    });
    expect(payload['columns']).toBeNull();
    expect(payload['options_companion']).toBeNull();
    expect(payload['adjust_for_dividends']).toBe(false);
    expect(payload['fail_on_gaps']).toBe(true);
    expect(payload['indicator_entries']).toEqual([]);
  });
});
