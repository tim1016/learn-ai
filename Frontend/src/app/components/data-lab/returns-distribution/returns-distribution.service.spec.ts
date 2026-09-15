import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { firstValueFrom } from 'rxjs';
import { beforeEach, describe, expect, it } from 'vitest';

import { environment } from '../../../../environments/environment';
import { ReturnsDistributionService } from './returns-distribution.service';

const BASE = `${environment.pythonServiceUrl}/api/research/return-distribution`;
const DAY_CANDLES_BASE = `${environment.pythonServiceUrl}/api/research/return-distribution/day-candles`;
const FROM_MS = Date.UTC(2024, 6, 1);
const TO_MS = Date.UTC(2025, 5, 30, 23, 59, 59, 999);

/** One complete snake_case response, small enough to verify every mapping. */
const RESPONSE_DTO = {
  meta: {
    symbol: 'SPY',
    from_ms_utc: FROM_MS,
    to_ms_utc: TO_MS,
    resolution: '1m',
    bin_width_pct: 0.5,
    span_pct: 5,
    adjustment: 'split_and_dividend',
    capture: {
      status: 'complete',
      fetched_artifact_count: 42,
      detail: null,
    },
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
      bin_indices: { close_to_close: 1, session: 1, overnight: null },
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

  it('posts the numeric ms window verbatim and maps the response to camelCase', async () => {
    const pending = firstValueFrom(
      service.distribution({
        symbol: 'SPY',
        fromMsUtc: FROM_MS,
        toMsUtc: TO_MS,
        binWidthPct: 0.5,
        spanPct: 5,
      }),
    );

    const request = http.expectOne(BASE);
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      symbol: 'SPY',
      from_ms_utc: FROM_MS,
      to_ms_utc: TO_MS,
      bin_width_pct: 0.5,
      span_pct: 5,
    });
    request.flush(RESPONSE_DTO);

    const study = await pending;
    expect(study.adjustment).toBe('split_and_dividend');
    expect(study.warnings).toHaveLength(1);
    expect(study.capture).toEqual({
      status: 'complete',
      fetchedArtifactCount: 42,
      detail: null,
    });
    expect(study.coverage.missingSessions).toBe(2);
    expect(study.coverage.firstSessionOpenMsUtc).toBe(1719821400000);

    const kind = study.kinds[0];
    expect(kind?.kind).toBe('close_to_close');
    expect(kind?.bins[0]?.isEdge).toBe(true);
    expect(kind?.bins[0]?.lowerPct).toBeNull();
    expect(kind.normalExpectedCounts[1]).toBeCloseTo(118.2, 10);
    expect(kind?.stats.annualizedVolPct).toBeCloseTo(15.56, 10);
    expect(kind?.stats.worstDay.valuePct).toBeCloseTo(-5.4, 10);

    const day = study.days[0];
    expect(day?.preMarketPct).toBeCloseTo(0.02, 10);
    expect(day?.afterHoursPct).toBeNull();
    expect(day?.volume).toBe(4_500_000);
    expect(day?.binIndices).toEqual({ close_to_close: 1, session: 1, overnight: null });
  });

  it('maps undefined stats to null — undefined is data, not zero', async () => {
    const dto = {
      ...RESPONSE_DTO,
      kinds: RESPONSE_DTO.kinds.map((k) => ({
        ...k,
        stats: { ...k.stats, skewness: null, excess_kurtosis: null },
      })),
    };
    const pending = firstValueFrom(
      service.distribution({
        symbol: 'SPY',
        fromMsUtc: FROM_MS,
        toMsUtc: TO_MS,
        binWidthPct: 0.5,
        spanPct: 5,
      }),
    );
    http.expectOne(BASE).flush(dto);
    const study = await pending;
    expect(study.kinds[0]?.stats.skewness).toBeNull();
    expect(study.kinds[0]?.stats.excessKurtosis).toBeNull();
  });

  it('requests the study day-candles read and maps generated-contract bars', async () => {
    const sessionOpenMs = 1719907800000;
    const pending = firstValueFrom(service.minuteCandles('SPY', sessionOpenMs));

    const request = http.expectOne(DAY_CANDLES_BASE);
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      symbol: 'SPY',
      session_open_ms_utc: sessionOpenMs,
    });
    request.flush({
      symbol: 'SPY',
      session_open_ms_utc: sessionOpenMs,
      adjustment: 'split_and_dividend',
      bars: [{ t: 1719907200000, o: 545.1, h: 545.2, l: 545.0, c: 545.15, v: 1200 }],
    });

    const bars = await pending;
    expect(bars).toHaveLength(1);
    const bar = bars[0];
    expect(bar?.timestamp).toBe(1719907200000);
    expect(bar?.open).toBeCloseTo(545.1, 10);
    expect(bar?.timespan).toBe('minute');
    expect(bar?.volumeWeightedAveragePrice).toBeNull();
  });
});
