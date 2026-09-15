import { HttpClient } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { map, type Observable } from 'rxjs';

import { components } from '../../../api/broker.types';
import { StockAggregate } from '../../../graphql/types';
import { environment } from '../../../../environments/environment';

type ReturnDistributionResponseDto = components['schemas']['ReturnDistributionResponse'];
type ReturnDistributionRequestDto = components['schemas']['ReturnDistributionRequest'];
type ChartDataRequestDto = components['schemas']['ChartDataRequest'];

export type ReturnKind = 'close_to_close' | 'session' | 'overnight';

export interface StudyQuery {
  symbol: string;
  fromDate: string;
  toDate: string;
  binWidthPct: number;
  spanPct: number;
}

export interface HistogramBin {
  lowerPct: number | null;
  upperPct: number | null;
  count: number;
  isEdge: boolean;
}

export interface ExtremeDay {
  sessionOpenMsUtc: number;
  valuePct: number;
}

export interface DistStats {
  nDays: number;
  meanPct: number;
  stdPct: number;
  annualizedVolPct: number;
  skewness: number;
  excessKurtosis: number;
  var95Pct: number;
  cvar95Pct: number;
  bestDay: ExtremeDay;
  worstDay: ExtremeDay;
}

export interface KindDistribution {
  kind: ReturnKind;
  bins: HistogramBin[];
  normalExpectedCounts: number[];
  stats: DistStats;
}

export interface DayReturns {
  sessionOpenMsUtc: number;
  closeToClosePct: number | null;
  sessionPct: number | null;
  overnightPct: number | null;
  preMarketPct: number | null;
  morningPct: number | null;
  afternoonPct: number | null;
  afterHoursPct: number | null;
  volume: number;
}

export interface StudyCoverage {
  requestedSessions: number;
  returnedSessions: number;
  missingSessions: number;
  excludedSessions: number;
  firstSessionOpenMsUtc: number | null;
  lastSessionOpenMsUtc: number | null;
}

export interface CaptureReceipt {
  attempted: boolean;
  status: string;
  fetchedArtifactCount: number;
  detail: string | null;
}

export interface ReturnDistributionStudy {
  adjustment: 'split_and_dividend' | 'raw';
  warnings: readonly string[];
  capture: CaptureReceipt | null;
  coverage: StudyCoverage;
  kinds: readonly KindDistribution[];
  days: readonly DayReturns[];
}

interface MinuteBarDto {
  timestamp: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

/** The explicit boundary adapter: the wire DTO is Python's snake_case; the
 * page's model is camelCase. Mapping only — every number arrives computed. */
function toStudy(dto: ReturnDistributionResponseDto): ReturnDistributionStudy {
  return {
    adjustment: dto.meta.adjustment,
    warnings: dto.meta.warnings ?? [],
    capture: dto.meta.capture
      ? {
          attempted: dto.meta.capture.attempted,
          status: dto.meta.capture.status,
          fetchedArtifactCount: dto.meta.capture.fetched_artifact_count,
          detail: dto.meta.capture.detail ?? null,
        }
      : null,
    coverage: {
      requestedSessions: dto.coverage.requested_sessions,
      returnedSessions: dto.coverage.returned_sessions,
      missingSessions: dto.coverage.missing_sessions,
      excludedSessions: dto.coverage.excluded_sessions,
      firstSessionOpenMsUtc: dto.coverage.first_session_open_ms_utc,
      lastSessionOpenMsUtc: dto.coverage.last_session_open_ms_utc,
    },
    kinds: dto.kinds.map((k) => ({
      kind: k.kind,
      bins: k.bins.map((b) => ({
        lowerPct: b.lower_pct,
        upperPct: b.upper_pct,
        count: b.count,
        isEdge: b.is_edge,
      })),
      normalExpectedCounts: k.normal_expected_counts,
      stats: {
        nDays: k.stats.n_days,
        meanPct: k.stats.mean_pct,
        stdPct: k.stats.std_pct,
        annualizedVolPct: k.stats.annualized_vol_pct,
        skewness: k.stats.skewness,
        excessKurtosis: k.stats.excess_kurtosis,
        var95Pct: k.stats.var_95_pct,
        cvar95Pct: k.stats.cvar_95_pct,
        bestDay: {
          sessionOpenMsUtc: k.stats.best_day.session_open_ms_utc,
          valuePct: k.stats.best_day.value_pct,
        },
        worstDay: {
          sessionOpenMsUtc: k.stats.worst_day.session_open_ms_utc,
          valuePct: k.stats.worst_day.value_pct,
        },
      },
    })),
    days: dto.days.map((d) => ({
      sessionOpenMsUtc: d.session_open_ms_utc,
      closeToClosePct: d.close_to_close_pct,
      sessionPct: d.session_pct,
      overnightPct: d.overnight_pct,
      preMarketPct: d.pre_market_pct,
      morningPct: d.morning_pct,
      afternoonPct: d.afternoon_pct,
      afterHoursPct: d.after_hours_pct,
      volume: d.volume,
    })),
  };
}

/**
 * Return-distribution study reads over FastAPI (ADR 0031 direct boundary).
 * Python authors every number — bins, stats, overlay, segments; this service
 * owns only the wire-to-model adapter and the day-candles side read.
 */
@Injectable({ providedIn: 'root' })
export class ReturnsDistributionService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.pythonServiceUrl}/api/research/return-distribution`;
  private readonly chartBase = `${environment.pythonServiceUrl}/api/chart/data`;

  distribution(query: StudyQuery): Observable<ReturnDistributionStudy> {
    const body: ReturnDistributionRequestDto = {
      symbol: query.symbol,
      from_date: query.fromDate,
      to_date: query.toDate,
      bin_width_pct: query.binWidthPct,
      span_pct: query.spanPct,
    };
    return this.http
      .post<ReturnDistributionResponseDto>(this.base, body)
      .pipe(map(toStudy));
  }

  /** One trading day's extended-session minute candles for the drill-down.
   *
   * The generated contract types `/api/chart/data`'s 200 body as `unknown`
   * (the route declares no response model), so this refines it to the six
   * keys both the lake and provider paths guarantee — the same bar contract
   * `data-lab-chart` consumes. */
  minuteCandles(ticker: string, isoDate: string): Observable<StockAggregate[]> {
    const body: ChartDataRequestDto = {
      ticker,
      from_date: isoDate,
      to_date: isoDate,
      timeframe: '1m',
      session: 'extended',
      adjusted: true,
      forward_fill: false,
      indicators: [],
    };
    return this.http.post<{ bars: MinuteBarDto[] }>(this.chartBase, body).pipe(
      map((response) =>
        response.bars.map((bar) => ({
          id: 0,
          tickerId: 0,
          open: bar.open,
          high: bar.high,
          low: bar.low,
          close: bar.close,
          volume: bar.volume,
          volumeWeightedAveragePrice: null,
          timestamp: bar.timestamp,
          timespan: 'minute',
          multiplier: 1,
          transactionCount: null,
        })),
      ),
    );
  }
}
