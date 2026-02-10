import { Injectable, inject } from '@angular/core';
import { Apollo } from 'apollo-angular';
import { map } from 'rxjs/operators';
import { Observable } from 'rxjs';
import { GET_OR_FETCH_STOCK_AGGREGATES } from '../graphql/queries';
import { GetOrFetchStockAggregatesResponse, SmartAggregatesResult } from '../graphql/types';

@Injectable({
  providedIn: 'root'
})
export class MarketDataService {
  private apollo = inject(Apollo);

  getOrFetchStockAggregates(
    ticker: string,
    fromDate: string,
    toDate: string,
    timespan: string = 'day',
    multiplier: number = 1
  ): Observable<SmartAggregatesResult> {
    return this.apollo
      .watchQuery<GetOrFetchStockAggregatesResponse>({
        query: GET_OR_FETCH_STOCK_AGGREGATES,
        variables: { ticker, fromDate, toDate, timespan, multiplier },
        fetchPolicy: 'network-only'
      })
      .valueChanges.pipe(
        map(result => result.data!.getOrFetchStockAggregates as SmartAggregatesResult)
      );
  }
}
