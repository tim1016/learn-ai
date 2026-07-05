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
    stockAggregateStats(symbol: $symbol) {
      count
      earliest
      latest
    }
  }
`;

interface GraphQLResponse<T> {
  data: T;
  errors?: { message: string }[];
}

interface StockAggregateStatsResponse {
  stockAggregateStats: {
    count: number;
    earliest: number | null;
    latest: number | null;
  };
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
      .post<GraphQLResponse<StockAggregateStatsResponse>>(GRAPHQL_URL, {
        query: GET_TICKER_STATS_QUERY,
        variables: { symbol }
      })
      .pipe(
        map(response => {
          const stats = response.data.stockAggregateStats;
          return {
            count: stats.count,
            earliest: stats.earliest,
            latest: stats.latest
          };
        })
      );
  }
}
