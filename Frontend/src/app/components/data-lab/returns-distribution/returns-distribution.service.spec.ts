import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { firstValueFrom } from 'rxjs';
import { beforeEach, describe, expect, it } from 'vitest';

import { environment } from '../../../../environments/environment';
import { ReturnsDistributionService } from './returns-distribution.service';

const BASE = `${environment.pythonServiceUrl}/api/research/return-distribution`;
const CHART_BASE = `${environment.pythonServiceUrl}/api/chart/data`;

/** One complete snake_case response, small enough to verify every mapping. */
const RESPONSE_DTO = {
  meta: {
    symbol: 'SPY',
    from_date: '2024-07-01',
    to_date: '2025-06-30',
    resolution: '1m',
    bin_width_pct: 0.5,
    span_pct: 5,
    adjustment: 'split_and_dividend',
    warnings: ['2 scheduled session(s) in the window are not captured in the lake; the study covers what is captured'],
  },
  coverage: {
    requested_sessions: 251,
    returned_sessions: 249,
    missing_sessions: 2,
    excluded_sessions: 0,
    first_session_open_ms_utc: 1719821400000,
    last_session_open_ms_utc: 1751275800000,
  },
  kinds: [
    {
      kind: 'close_to_close',
      bins: [
        { lower_pct: null, upper_pct: -5, count: 1, is_edge: true },
        { lower_pct: -0.5, upper_pct: 0, count: 120, is_edge: false },
      ],
      normal_expected_counts: [0.4, 118.2],
      stats: {
        n_days: 249,
        mean_pct: 0.042,
        std_pct: 0.98,
        annualized_vol_pct: 15.56,
        skewness: -0.21,
        excess_kurtosis: 1.7,
        var_95_pct: -1.52,
        cvar_95_pct: -2.11,
        best_day: { session_open_ms_utc: 1719821400000, value_pct: 2.9 },
        worst_day: { session_open_ms_utc: 1751275800000, value_pct: -5.4 },
      },
    },
  ],
  days: [
    {
      session_open_ms_utc: 1719907800000,
      close_to_close_pct: 0.25,
      session_pct: 0.31,
      overnight_pct: -0.06,
      pre_market_pct: 0.02,
      morning_pct: 0.19,
      afternoon_pct: 0.12,
      after_hours_pct: null,
      volume: 4_500_000,
    },
  ],
};

describe('ReturnsDistributionService', () => {
  let service: ReturnsDistributionService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(ReturnsDistributionService);
    http = TestBed.inject(HttpTestingController);
  });

  it('posts the study request as declared and maps the response to camelCase', async () => {
    const pending = firstValueFrom(
      service.distribution({
        symbol: 'SPY',
        fromDate: '2024-07-01',
        toDate: '2025-06-30',
        binWidthPct: 0.5,
        spanPct: 5,
      }),
    );

    const request = http.expectOne(BASE);
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      symbol: 'SPY',
      from_date: '2024-07-01',
      to_date: '2025-06-30',
      bin_width_pct: 0.5,
      span_pct: 5,
    });
    request.flush(RESPONSE_DTO);

    const study = await pending;
    expect(study.adjustment).toBe('split_and_dividend');
    expect(study.warnings).toHaveLength(1);
    expect(study.coverage.missingSessions).toBe(2);
    expect(study.coverage.firstSessionOpenMsUtc).toBe(1719821400000);

    const kind = study.kinds[0]!;
    expect(kind.kind).toBe('close_to_close');
    expect(kind.bins[0]!.isEdge).toBe(true);
    expect(kind.bins[0]!.lowerPct).toBeNull();
    expect(kind.normalExpectedCounts[1]).toBeCloseTo(118.2, 10);
    expect(kind.stats.annualizedVolPct).toBeCloseTo(15.56, 10);
    expect(kind.stats.worstDay.valuePct).toBeCloseTo(-5.4, 10);

    const day = study.days[0]!;
    expect(day.preMarketPct).toBeCloseTo(0.02, 10);
    expect(day.afterHoursPct).toBeNull();
    expect(day.volume).toBe(4_500_000);
  });

  it('requests a single-day extended 1m window for the candle pane and maps bars', async () => {
    const pending = firstValueFrom(service.minuteCandles('SPY', '2024-07-02'));

    const request = http.expectOne(CHART_BASE);
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      ticker: 'SPY',
      from_date: '2024-07-02',
      to_date: '2024-07-02',
      timeframe: '1m',
      session: 'extended',
      adjusted: true,
      forward_fill: false,
      indicators: [],
    });
    request.flush({
      bars: [
        { timestamp: 1719907200000, open: 545.1, high: 545.2, low: 545.0, close: 545.15, volume: 1200 },
      ],
    });

    const bars = await pending;
    expect(bars).toHaveLength(1);
    expect(bars[0]!.timestamp).toBe(1719907200000);
    expect(bars[0]!.timespan).toBe('minute');
    expect(bars[0]!.volumeWeightedAveragePrice).toBeNull();
  });
});
