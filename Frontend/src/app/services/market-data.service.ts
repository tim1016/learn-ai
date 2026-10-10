import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { map, tap } from 'rxjs/operators';
import { Observable } from 'rxjs';
import {
  SmartAggregatesResult,
  OptionsChainSnapshotResult, StockSnapshotResult,
  StrategyAnalyzeResult, StrategyAnalyzeOptions, StrategyLegInput,
  PricingCompareResult,
} from '../graphql/types';
import { environment } from '../../environments/environment';
import { etIsoDate, shiftIsoDateByMonths } from '../shared/date/et-midnight';
import { optionExpiryAnchorMs } from '../shared/option-contract/option-expiry-date';

const GRAPHQL_URL = environment.backendUrl;

const QUERY = `
  query GetOrFetchStockAggregates(
    $ticker: String!
    $fromDate: String!
    $toDate: String!
    $timespan: String! = "day"
    $multiplier: Int! = 1
    $forceRefresh: Boolean! = false
    $adjusted: Boolean! = true
  ) {
    getOrFetchStockAggregates(
      ticker: $ticker
      fromDate: $fromDate
      toDate: $toDate
      timespan: $timespan
      multiplier: $multiplier
      forceRefresh: $forceRefresh
      adjusted: $adjusted
    ) {
      ticker
      aggregates {
        id open high low close volume
        volumeWeightedAveragePrice timestamp
        timespan multiplier transactionCount
      }
    }
  }
`;

interface GraphQLResponse {
  data: { getOrFetchStockAggregates: SmartAggregatesResult };
  errors?: { message: string }[];
}

const GET_OPTIONS_EXPIRATIONS_QUERY = `
  query GetOptionsExpirations(
    $underlyingTicker: String!
    $contractType: String
    $expirationMsGte: Long
    $expirationMsLte: Long
  ) {
    getOptionsExpirations(
      underlyingTicker: $underlyingTicker
      contractType: $contractType
      expirationMsGte: $expirationMsGte
      expirationMsLte: $expirationMsLte
    ) {
      success
      expirations
      count
      error
    }
  }
`;

interface OptionsExpirationsResponse {
  data: { getOptionsExpirations: { success: boolean; expirations: number[]; count: number; error?: string } };
  errors?: { message: string }[];
}

const GET_OPTIONS_CHAIN_SNAPSHOT_QUERY = `
  query GetOptionsChainSnapshot($underlyingTicker: String!, $expirationMs: Long) {
    getOptionsChainSnapshot(underlyingTicker: $underlyingTicker, expirationMs: $expirationMs) {
      success
      underlying {
        ticker price change changePercent
      }
      contracts {
        ticker contractType strikePrice expirationMs
        breakEvenPrice impliedVolatility openInterest
        greeks { delta gamma theta vega }
        day { open high low close volume vwap }
        lastTrade { price size exchange timeframe }
        lastQuote { bid ask bidSize askSize midpoint timeframe }
      }
      count
      riskFreeRate
      dividendYield
      rateSource
      dividendSource
      error
    }
  }
`;

interface OptionsChainSnapshotResponse {
  data: { getOptionsChainSnapshot: OptionsChainSnapshotResult };
  errors?: { message: string }[];
}

const SNAPSHOT_FIELDS = `
  ticker
  day { open high low close volume vwap }
  prevDay { open high low close volume vwap }
  min { open high low close volume vwap accumulatedVolume timestamp }
  todaysChange todaysChangePercent updated
`;

const GET_STOCK_SNAPSHOT_QUERY = `
  query GetStockSnapshot($ticker: String!) {
    getStockSnapshot(ticker: $ticker) {
      success
      snapshot { ${SNAPSHOT_FIELDS} }
      error
    }
  }
`;

interface StockSnapshotResponse {
  data: { getStockSnapshot: StockSnapshotResult };
  errors?: { message: string }[];
}

const ANALYZE_OPTIONS_STRATEGY_QUERY = `
  query AnalyzeOptionsStrategy(
    $symbol: String!
    $legs: [StrategyLegInput!]!
    $expirationMs: Long!
    $spotPrice: Decimal!
    $riskFreeRate: Decimal
    $includeCurrentCurve: Boolean = false
    $includeGreekCurves: Boolean = false
    $includeLegDiagnostics: Boolean = false
    $whatIfTimeShiftDays: Decimal = 0
    $whatIfIvShift: Decimal = 0
  ) {
    analyzeOptionsStrategy(
      symbol: $symbol
      legs: $legs
      expirationMs: $expirationMs
      spotPrice: $spotPrice
      riskFreeRate: $riskFreeRate
      includeCurrentCurve: $includeCurrentCurve
      includeGreekCurves: $includeGreekCurves
      includeLegDiagnostics: $includeLegDiagnostics
      whatIfTimeShiftDays: $whatIfTimeShiftDays
      whatIfIvShift: $whatIfIvShift
    ) {
      success symbol spotPrice strategyCost
      pop expectedValue maxProfit maxLoss breakevens
      curve { price pnl }
      greeks { delta gamma theta vega }
      currentCurve { price theoreticalValue theoreticalPnl }
      greekCurves { price delta gamma theta vega }
      legDiagnostics {
        legId strike optionType position quantity iv entryPremium
        currentTheoretical currentDelta currentGamma currentTheta currentVega
        legPnl
      }
      error
    }
  }
`;

interface AnalyzeOptionsStrategyResponse {
  data: { analyzeOptionsStrategy: StrategyAnalyzeResult };
  errors?: { message: string }[];
}

const PRICING_MODEL_COMPARISON_QUERY = `
  query PricingModelComparison(
    $spot: Decimal!
    $strike: Decimal!
    $volatility: Decimal!
    $expirationMs: Long!
    $optionType: String!
    $riskFreeRate: Decimal
    $dividendYield: Decimal = 0
    $evaluationDate: String
    $spotMin: Decimal
    $spotMax: Decimal
    $numPoints: Int = 100
  ) {
    pricingModelComparison(
      spot: $spot
      strike: $strike
      volatility: $volatility
      expirationMs: $expirationMs
      optionType: $optionType
      riskFreeRate: $riskFreeRate
      dividendYield: $dividendYield
      evaluationDate: $evaluationDate
      spotMin: $spotMin
      spotMax: $spotMax
      numPoints: $numPoints
    ) {
      success
      strike
      optionType
      expirationMs
      timeToExpiryYears
      riskFreeRate
      models {
        model
        points { spot price delta gamma theta vega rho }
      }
      error
    }
  }
`;

interface PricingModelComparisonResponse {
  data: { pricingModelComparison: PricingCompareResult };
  errors?: { message: string }[];
}

@Injectable({
  providedIn: 'root'
})
export class MarketDataService {
  private http = inject(HttpClient);

  getOrFetchStockAggregates(
    ticker: string,
    fromDate: string,
    toDate: string,
    timespan = 'day',
    multiplier = 1,
    forceRefresh = false,
    adjusted = true
  ): Observable<SmartAggregatesResult> {
    return this.http
      .post<GraphQLResponse>(GRAPHQL_URL, {
        query: QUERY,
        variables: { ticker, fromDate, toDate, timespan, multiplier, forceRefresh, adjusted }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getOrFetchStockAggregates)
      );
  }

  getOptionsExpirations(
    underlyingTicker: string,
    options: {
      contractType?: string;
      /** Bounds are expiry anchor instants (16:00 New York time on the date). */
      expirationMsGte?: number;
      expirationMsLte?: number;
    } = {}
  ): Observable<number[]> {
    return this.http
      .post<OptionsExpirationsResponse>(GRAPHQL_URL, {
        query: GET_OPTIONS_EXPIRATIONS_QUERY,
        variables: {
          underlyingTicker,
          contractType: options.contractType,
          expirationMsGte: options.expirationMsGte ?? optionExpiryAnchorMs(etIsoDate(Date.now())),
          // Both bounds are anchors of ET dates — one zone, six calendar
          // months out (clamped month arithmetic, not 183 24-hour periods).
          expirationMsLte: options.expirationMsLte ?? optionExpiryAnchorMs(shiftIsoDateByMonths(etIsoDate(Date.now()), 6)),
        }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => {
          const result = response.data.getOptionsExpirations;
          return result.success ? result.expirations : [];
        })
      );
  }

  getOptionsChainSnapshot(
    underlyingTicker: string,
    expirationMs?: number
  ): Observable<OptionsChainSnapshotResult> {
    return this.http
      .post<OptionsChainSnapshotResponse>(GRAPHQL_URL, {
        query: GET_OPTIONS_CHAIN_SNAPSHOT_QUERY,
        variables: { underlyingTicker, expirationMs }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getOptionsChainSnapshot)
      );
  }

  getStockSnapshot(ticker: string): Observable<StockSnapshotResult> {
    return this.http
      .post<StockSnapshotResponse>(GRAPHQL_URL, {
        query: GET_STOCK_SNAPSHOT_QUERY,
        variables: { ticker }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getStockSnapshot)
      );
  }

  analyzeOptionsStrategy(
    symbol: string,
    legs: StrategyLegInput[],
    /** The expiry anchor instant: 16:00 New York time on the expiry date. */
    expirationMs: number,
    spotPrice: number,
    /** Omit to have Python price at its one default rate (#2764). */
    riskFreeRate: number | null = null,
    options: StrategyAnalyzeOptions = {},
  ): Observable<StrategyAnalyzeResult> {
    return this.http
      .post<AnalyzeOptionsStrategyResponse>(GRAPHQL_URL, {
        query: ANALYZE_OPTIONS_STRATEGY_QUERY,
        variables: {
          symbol, legs, expirationMs, spotPrice, riskFreeRate,
          includeCurrentCurve: options.includeCurrentCurve ?? false,
          includeGreekCurves: options.includeGreekCurves ?? false,
          includeLegDiagnostics: options.includeLegDiagnostics ?? false,
          whatIfTimeShiftDays: options.whatIfTimeShiftDays ?? 0,
          whatIfIvShift: options.whatIfIvShift ?? 0,
        }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.analyzeOptionsStrategy)
      );
  }

  comparePricingModels(params: {
    spot: number;
    strike: number;
    volatility: number;
    /** The expiry anchor instant: 16:00 New York time on the expiry date. */
    expirationMs: number;
    optionType: string;
    riskFreeRate?: number;
    dividendYield?: number;
    evaluationDate?: string;
    spotMin?: number;
    spotMax?: number;
    numPoints?: number;
  }): Observable<PricingCompareResult> {
    return this.http
      .post<PricingModelComparisonResponse>(GRAPHQL_URL, {
        query: PRICING_MODEL_COMPARISON_QUERY,
        variables: {
          spot: params.spot,
          strike: params.strike,
          volatility: params.volatility,
          expirationMs: params.expirationMs,
          optionType: params.optionType,
          riskFreeRate: params.riskFreeRate ?? null,
          dividendYield: params.dividendYield ?? 0,
          evaluationDate: params.evaluationDate ?? null,
          spotMin: params.spotMin ?? null,
          spotMax: params.spotMax ?? null,
          numPoints: params.numPoints ?? 100,
        },
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.pricingModelComparison),
      );
  }
}
