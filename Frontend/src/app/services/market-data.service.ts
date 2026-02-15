import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { map, tap } from 'rxjs/operators';
import { Observable } from 'rxjs';
import { SmartAggregatesResult, CalculateIndicatorsResult } from '../graphql/types';

const GRAPHQL_URL = 'http://localhost:5000/graphql';

const QUERY = `
  query GetOrFetchStockAggregates(
    $ticker: String!
    $fromDate: String!
    $toDate: String!
    $timespan: String! = "day"
    $multiplier: Int! = 1
    $forceRefresh: Boolean! = false
  ) {
    getOrFetchStockAggregates(
      ticker: $ticker
      fromDate: $fromDate
      toDate: $toDate
      timespan: $timespan
      multiplier: $multiplier
      forceRefresh: $forceRefresh
    ) {
      ticker
      aggregates {
        id open high low close volume
        volumeWeightedAveragePrice timestamp
        timespan multiplier transactionCount
      }
      summary {
        periodHigh periodLow averageVolume averageVwap
        openPrice closePrice priceChange priceChangePercent totalBars
      }
    }
  }
`;

interface GraphQLResponse {
  data: { getOrFetchStockAggregates: SmartAggregatesResult };
  errors?: { message: string }[];
}

const CALCULATE_INDICATORS_QUERY = `
  query CalculateIndicators(
    $ticker: String!
    $fromDate: String!
    $toDate: String!
    $indicators: [IndicatorConfigInput!]!
    $timespan: String! = "day"
    $multiplier: Int! = 1
  ) {
    calculateIndicators(
      ticker: $ticker
      fromDate: $fromDate
      toDate: $toDate
      indicators: $indicators
      timespan: $timespan
      multiplier: $multiplier
    ) {
      success
      ticker
      indicators {
        name
        window
        data {
          timestamp value signal histogram upper lower
        }
      }
      message
    }
  }
`;

const CHECK_CACHED_RANGES_QUERY = `
  query CheckCachedRanges(
    $ticker: String!
    $ranges: [DateRangeInput!]!
    $timespan: String! = "day"
    $multiplier: Int! = 1
  ) {
    checkCachedRanges(
      ticker: $ticker
      ranges: $ranges
      timespan: $timespan
      multiplier: $multiplier
    ) {
      fromDate toDate isCached
    }
  }
`;

interface CheckCachedRangesResponse {
  data: { checkCachedRanges: CachedRangeResult[] };
  errors?: { message: string }[];
}

export interface CachedRangeResult {
  fromDate: string;
  toDate: string;
  isCached: boolean;
}

interface CalculateIndicatorsResponse {
  data: { calculateIndicators: CalculateIndicatorsResult };
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
    timespan: string = 'day',
    multiplier: number = 1,
    forceRefresh: boolean = false
  ): Observable<SmartAggregatesResult> {
    console.log('[STEP 1.5 - Service] Sending GraphQL query:', {
      ticker, fromDate, toDate, timespan, multiplier, forceRefresh
    });

    return this.http
      .post<GraphQLResponse>(GRAPHQL_URL, {
        query: QUERY,
        variables: { ticker, fromDate, toDate, timespan, multiplier, forceRefresh }
      })
      .pipe(
        tap(response => {
          console.log('[STEP 1.7 - Service] GraphQL response:', {
            hasData: !!response.data,
            errors: response.errors,
            result: response.data?.getOrFetchStockAggregates
          });
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getOrFetchStockAggregates)
      );
  }

  checkCachedRanges(
    ticker: string,
    ranges: { fromDate: string; toDate: string }[],
    timespan: string = 'day',
    multiplier: number = 1
  ): Observable<CachedRangeResult[]> {
    return this.http
      .post<CheckCachedRangesResponse>(GRAPHQL_URL, {
        query: CHECK_CACHED_RANGES_QUERY,
        variables: { ticker, ranges, timespan, multiplier }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.checkCachedRanges)
      );
  }

  calculateIndicators(
    ticker: string,
    fromDate: string,
    toDate: string,
    indicators: { name: string; window: number }[],
    timespan: string = 'day',
    multiplier: number = 1
  ): Observable<CalculateIndicatorsResult> {
    return this.http
      .post<CalculateIndicatorsResponse>(GRAPHQL_URL, {
        query: CALCULATE_INDICATORS_QUERY,
        variables: { ticker, fromDate, toDate, indicators, timespan, multiplier }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.calculateIndicators)
      );
  }
}
