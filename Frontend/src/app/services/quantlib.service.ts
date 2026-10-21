import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import type { components } from '../api/broker.types';
import {
  QuantLibPriceResult,
  QuantLibEngine,
} from './market-data.types';
import { failedQuantLibPrice } from './market-data.mappers';
import { failureAsResult } from './python-http-failure';
import { environment } from '../../environments/environment';

@Injectable({ providedIn: 'root' })
export class QuantLibService {
  private readonly http = inject(HttpClient);
  private readonly url = `${environment.pythonServiceUrl}/api/quantlib/price`;

  /** Selected QuantLib sub-engine for comparison. */
  readonly selectedEngine = signal<QuantLibEngine>('analytic_bs');

  /**
   * `POST /api/quantlib/price`. Python reports an engine failure in-band
   * (`success: false`); an HTTP failure resolves the same way.
   */
  async priceOption(params: {
    spot: number;
    strike: number;
    volatility: number;
    /** The expiry anchor instant: 16:00 New York time on the expiry date. */
    expirationMs: number;
    optionType: 'call' | 'put';
    /** Omit to have Python price at its one default rate (#2764). */
    riskFreeRate?: number;
    evaluationDate?: string;
    dividendYield?: number;
    engine?: QuantLibEngine;
  }): Promise<QuantLibPriceResult> {
    const engine = params.engine ?? this.selectedEngine();
    // `undefined` is dropped by JSON; Python rejects a `null` for the rate and the date.
    const body: components['schemas']['QuantLibPriceRequest'] = {
      spot: params.spot,
      strike: params.strike,
      volatility: params.volatility,
      expiration_ms: params.expirationMs,
      option_type: params.optionType,
      risk_free_rate: params.riskFreeRate,
      evaluation_date: params.evaluationDate,
      dividend_yield: params.dividendYield ?? 0,
      engine,
    };
    return firstValueFrom(
      this.http.post<QuantLibPriceResult>(this.url, body).pipe(failureAsResult(failedQuantLibPrice(engine))),
    );
  }
}
