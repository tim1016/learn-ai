import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { map, tap } from 'rxjs/operators';
import { Observable } from 'rxjs';
import { Ticker } from '../graphql/types';

const GRAPHQL_URL = 'http://localhost:5000/graphql';

const GET_TICKERS_QUERY = `
  query GetTickers {
    tickers {
      id
      symbol
      name
      market
      active
      createdAt
      updatedAt
      sanitizationSummary
    }
  }
`;

const GET_TICKER_STATS_QUERY = `
  query GetTickerStats($symbol: String!) {
    stockAggregates(
      where: { ticker: { symbol: { eq: $symbol } } }
      order: [{ timestamp: ASC }]
    ) {
      timestamp
    }
  }
`;

interface GraphQLResponse<T> {
  data: T;
  errors?: { message: string }[];
}

interface StockAggregateTimestampRow {
  timestamp: string;
}

const GRAPHQL_DATE_TIME_WITH_OFFSET = /(?:Z|[+-]\d{2}:\d{2})$/;

function parseGraphQLDateTimeMs(value: string): number {
  if (!GRAPHQL_DATE_TIME_WITH_OFFSET.test(value)) {
    throw new Error(`stockAggregates.timestamp must include a timezone offset: ${value}`);
  }
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) {
    throw new Error(`stockAggregates.timestamp is not a valid DateTime scalar: ${value}`);
  }
  return ms;
}

@Injectable({
  providedIn: 'root'
})
export class TickerService {
  private http = inject(HttpClient);

  getTickers(): Observable<Ticker[]> {
    return this.http
      .post<GraphQLResponse<{ tickers: Ticker[] }>>(GRAPHQL_URL, {
        query: GET_TICKERS_QUERY
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.tickers)
      );
  }

  getAggregateStats(symbol: string): Observable<{ count: number; earliest: number | null; latest: number | null }> {
    return this.http
      .post<GraphQLResponse<{ stockAggregates: StockAggregateTimestampRow[] }>>(GRAPHQL_URL, {
        query: GET_TICKER_STATS_QUERY,
        variables: { symbol }
      })
      .pipe(
        map(response => {
          const aggs = response.data.stockAggregates ?? [];
          const timestamps = aggs.map(agg => parseGraphQLDateTimeMs(agg.timestamp));
          return {
            count: aggs.length,
            earliest: timestamps.length > 0 ? timestamps[0] : null,
            latest: timestamps.length > 0 ? timestamps[timestamps.length - 1] : null
          };
        })
      );
  }
}
