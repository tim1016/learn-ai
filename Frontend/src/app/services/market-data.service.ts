import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { map } from 'rxjs/operators';
import type { components } from '../api/broker.types';
import {
  StockAggregatesResult,
  OptionsChainSnapshotResult, StockSnapshotResult,
  StrategyAnalyzeResult, StrategyAnalyzeOptions, StrategyLegInput,
  PricingCompareParams, PricingCompareResult,
} from './market-data.types';
import {
  failedOptionsChainSnapshot, failedPricingComparison, failedStockSnapshot, failedStrategyAnalysis,
  toOptionsChainSnapshotResult, toPricingCompareRequest, toPricingCompareResult,
  toStockSnapshotResult, toStrategyAnalyzeRequest, toStrategyAnalyzeResult,
} from './market-data.mappers';
import { failureAsError, failureAsResult } from './python-http-failure';
import { environment } from '../../environments/environment';
import { etIsoDate, shiftIsoDateByMonths } from '../shared/date/et-midnight';
import { optionExpiryAnchorMs } from '../shared/option-contract/option-expiry-date';

type Schema = components['schemas'];

/**
 * Python's market-data and pricing routes, called directly.
 *
 * Python answers a failed call with an HTTP error and a `detail`. Two operations
 * reject with an `Error` carrying that sentence (aggregates, expirations); the four
 * whose screens read `success` and `error` resolve with `success: false` instead,
 * so one `!result.success` branch covers every kind of failure.
 */
@Injectable({
  providedIn: 'root'
})
export class MarketDataService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.pythonServiceUrl}/api`;

  /** `POST /api/aggregates/fetch` — always a live Polygon read; nothing is cached. */
  getStockAggregates(
    ticker: string,
    fromDate: string,
    toDate: string,
    timespan = 'day',
    multiplier = 1,
    adjusted = true
  ): Observable<StockAggregatesResult> {
    const body: Schema['AggregateRequest'] = {
      ticker, from_date: fromDate, to_date: toDate, timespan, multiplier, adjusted,
    };
    return this.http
      .post<Schema['SanitizedDataResponse']>(`${this.base}/aggregates/fetch`, body)
      .pipe(
        failureAsError(),
        map(response => {
          if (!response.success) {
            throw new Error(`Aggregates unavailable: ${response.error ?? 'no reason given'}`);
          }
          return { ticker: response.ticker.toUpperCase(), aggregates: response.data };
        }),
      );
  }

  /** `POST /api/options/expirations` — expiry anchor instants, rejecting when the lookup fails. */
  getOptionsExpirations(
    underlyingTicker: string,
    options: {
      contractType?: string;
      /** Bounds are expiry anchor instants (16:00 New York time on the date). */
      expirationMsGte?: number;
      expirationMsLte?: number;
    } = {}
  ): Observable<number[]> {
    const body: Schema['OptionsExpirationsRequest'] = {
      underlying_ticker: underlyingTicker,
      contract_type: options.contractType,
      expiration_ms_gte: options.expirationMsGte ?? optionExpiryAnchorMs(etIsoDate(Date.now())),
      // Both bounds are anchors of ET dates — one zone, six calendar
      // months out (clamped month arithmetic, not 183 24-hour periods).
      expiration_ms_lte: options.expirationMsLte ?? optionExpiryAnchorMs(shiftIsoDateByMonths(etIsoDate(Date.now()), 6)),
    };
    return this.http
      .post<Schema['OptionsExpirationsResponse']>(`${this.base}/options/expirations`, body)
      .pipe(
        failureAsError('Option expirations unavailable'),
        map(result => {
          // A vendor failure is not a ticker with no listed options: throw, so
          // every caller shows the reason instead of an empty ribbon.
          if (!result.success) {
            throw new Error(`Option expirations unavailable: ${result.error ?? 'no reason given'}`);
          }
          return result.expirations ?? [];
        }),
      );
  }

  /** `POST /api/snapshot/options-chain` */
  getOptionsChainSnapshot(
    underlyingTicker: string,
    expirationMs?: number
  ): Observable<OptionsChainSnapshotResult> {
    const body: Schema['OptionsChainSnapshotRequest'] = {
      underlying_ticker: underlyingTicker,
      expiration_ms: expirationMs,
    };
    return this.http
      .post<Schema['OptionsChainSnapshotResponse']>(`${this.base}/snapshot/options-chain`, body)
      .pipe(map(toOptionsChainSnapshotResult), failureAsResult(failedOptionsChainSnapshot));
  }

  /** `POST /api/snapshot/ticker` */
  getStockSnapshot(ticker: string): Observable<StockSnapshotResult> {
    const body: Schema['StockSnapshotRequest'] = { ticker };
    return this.http
      .post<Schema['StockSnapshotResponse']>(`${this.base}/snapshot/ticker`, body)
      .pipe(map(toStockSnapshotResult), failureAsResult(failedStockSnapshot));
  }

  /** `POST /api/strategy/analyze` */
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
    const body = toStrategyAnalyzeRequest(symbol, legs, expirationMs, spotPrice, riskFreeRate, options);
    return this.http
      .post<Schema['StrategyAnalyzeResponse']>(`${this.base}/strategy/analyze`, body)
      .pipe(map(toStrategyAnalyzeResult), failureAsResult(failedStrategyAnalysis));
  }

  /** `POST /api/quantlib/compare` */
  comparePricingModels(params: PricingCompareParams): Observable<PricingCompareResult> {
    return this.http
      .post<Schema['PricingCompareResponse']>(`${this.base}/quantlib/compare`, toPricingCompareRequest(params))
      .pipe(map(toPricingCompareResult), failureAsResult(failedPricingComparison(params)));
  }
}
