import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { map } from 'rxjs/operators';
import {
  QuantLibPriceResult,
  QuantLibEngine,
} from '../graphql/types';
import { environment } from '../../environments/environment';

const GRAPHQL_URL = environment.backendUrl;

// ---------------------------------------------------------------------------
// GraphQL query strings
// ---------------------------------------------------------------------------

const QUANTLIB_PRICE_QUERY = `
  query QuantLibPrice(
    $spot: Decimal!
    $strike: Decimal!
    $volatility: Decimal!
    $expirationDate: String!
    $optionType: String!
    $riskFreeRate: Decimal
    $evaluationDate: String
    $dividendYield: Decimal = 0
    $engine: String = "analytic_bs"
  ) {
    quantlibPrice(
      spot: $spot
      strike: $strike
      volatility: $volatility
      expirationDate: $expirationDate
      optionType: $optionType
      riskFreeRate: $riskFreeRate
      evaluationDate: $evaluationDate
      dividendYield: $dividendYield
      engine: $engine
    ) {
      success
      engine
      price
      delta
      gamma
      theta
      vega
      rho
      d1
      d2
      error
    }
  }
`;

// ---------------------------------------------------------------------------
// Service
// ---------------------------------------------------------------------------

@Injectable({ providedIn: 'root' })
export class QuantLibService {
  private readonly http = inject(HttpClient);

  /** Selected QuantLib sub-engine for comparison. */
  readonly selectedEngine = signal<QuantLibEngine>('analytic_bs');

  async priceOption(params: {
    spot: number;
    strike: number;
    volatility: number;
    expirationDate: string;
    optionType: 'call' | 'put';
    riskFreeRate?: number;
    evaluationDate?: string;
    dividendYield?: number;
    engine?: QuantLibEngine;
  }): Promise<QuantLibPriceResult> {
    return firstValueFrom(
      this.http
        .post<{ data: { quantlibPrice: QuantLibPriceResult } }>(GRAPHQL_URL, {
          query: QUANTLIB_PRICE_QUERY,
          variables: {
            spot: params.spot,
            strike: params.strike,
            volatility: params.volatility,
            expirationDate: params.expirationDate,
            optionType: params.optionType,
            riskFreeRate: params.riskFreeRate ?? null,
            evaluationDate: params.evaluationDate ?? null,
            dividendYield: params.dividendYield ?? 0,
            engine: params.engine ?? this.selectedEngine(),
          },
        })
        .pipe(map((r) => r.data.quantlibPrice)),
    );
  }
}
