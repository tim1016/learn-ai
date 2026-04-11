import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { map } from 'rxjs/operators';
import {
  QuantLibStatusResult,
  QuantLibPriceResult,
  QuantLibStrategyResult,
  QuantLibEngine,
  StrategyLegInput,
} from '../graphql/types';
import { environment } from '../../environments/environment';

const GRAPHQL_URL = environment.backendUrl;

// ---------------------------------------------------------------------------
// GraphQL query strings
// ---------------------------------------------------------------------------

const QUANTLIB_STATUS_QUERY = `
  query QuantLibStatus {
    quantlibStatus {
      available
      version
      engines
    }
  }
`;

const QUANTLIB_PRICE_QUERY = `
  query QuantLibPrice(
    $spot: Decimal!
    $strike: Decimal!
    $volatility: Decimal!
    $expirationDate: String!
    $optionType: String!
    $riskFreeRate: Decimal = 0.05
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

const QUANTLIB_STRATEGY_QUERY = `
  query QuantLibStrategy(
    $spot: Decimal!
    $legs: [StrategyLegInput!]!
    $expirationDate: String!
    $riskFreeRate: Decimal = 0.05
    $evaluationDate: String
    $dividendYield: Decimal = 0
    $engine: String = "analytic_bs"
  ) {
    quantlibStrategy(
      spot: $spot
      legs: $legs
      expirationDate: $expirationDate
      riskFreeRate: $riskFreeRate
      evaluationDate: $evaluationDate
      dividendYield: $dividendYield
      engine: $engine
    ) {
      success
      engine
      netPrice
      netDelta
      netGamma
      netTheta
      netVega
      netRho
      legs {
        engine
        price
        delta
        gamma
        theta
        vega
        rho
        d1
        d2
      }
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

  /** Reactive status — checked once on first use. */
  readonly available = signal<boolean | null>(null);
  readonly version = signal<string | null>(null);
  readonly engines = signal<string[]>([]);

  /** Selected QuantLib sub-engine for comparison. */
  readonly selectedEngine = signal<QuantLibEngine>('analytic_bs');

  async checkStatus(): Promise<QuantLibStatusResult> {
    const result = await firstValueFrom(
      this.http
        .post<{ data: { quantlibStatus: QuantLibStatusResult } }>(GRAPHQL_URL, {
          query: QUANTLIB_STATUS_QUERY,
        })
        .pipe(map((r) => r.data.quantlibStatus)),
    );
    this.available.set(result.available);
    this.version.set(result.version);
    this.engines.set(result.engines);
    return result;
  }

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
            riskFreeRate: params.riskFreeRate ?? 0.05,
            evaluationDate: params.evaluationDate ?? null,
            dividendYield: params.dividendYield ?? 0,
            engine: params.engine ?? this.selectedEngine(),
          },
        })
        .pipe(map((r) => r.data.quantlibPrice)),
    );
  }

  async priceStrategy(params: {
    spot: number;
    legs: StrategyLegInput[];
    expirationDate: string;
    riskFreeRate?: number;
    evaluationDate?: string;
    dividendYield?: number;
    engine?: QuantLibEngine;
  }): Promise<QuantLibStrategyResult> {
    return firstValueFrom(
      this.http
        .post<{ data: { quantlibStrategy: QuantLibStrategyResult } }>(GRAPHQL_URL, {
          query: QUANTLIB_STRATEGY_QUERY,
          variables: {
            spot: params.spot,
            legs: params.legs,
            expirationDate: params.expirationDate,
            riskFreeRate: params.riskFreeRate ?? 0.05,
            evaluationDate: params.evaluationDate ?? null,
            dividendYield: params.dividendYield ?? 0,
            engine: params.engine ?? this.selectedEngine(),
          },
        })
        .pipe(map((r) => r.data.quantlibStrategy)),
    );
  }
}
