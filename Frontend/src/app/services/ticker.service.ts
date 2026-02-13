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

  getAggregateStats(symbol: string): Observable<{ count: number; earliest: string | null; latest: string | null }> {
    return this.http
      .post<GraphQLResponse<{ stockAggregates: { timestamp: string }[] }>>(GRAPHQL_URL, {
        query: GET_TICKER_STATS_QUERY,
        variables: { symbol }
      })
      .pipe(
        map(response => {
          const aggs = response.data.stockAggregates ?? [];
          return {
            count: aggs.length,
            earliest: aggs.length > 0 ? aggs[0].timestamp : null,
            latest: aggs.length > 0 ? aggs[aggs.length - 1].timestamp : null
          };
        })
      );
  }
}
