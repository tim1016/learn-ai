import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../environments/environment';
import {
  SpecBacktestRequest,
  SpecStrategyBacktestResult,
  StrategySpec,
} from '../graphql/spec-strategy.models';

/** Run configuration the runner supplies alongside the spec. */
export interface SpecBacktestRunOptions {
  startDate: string;
  endDate: string;
  initialCash?: number;
  fillMode?: 'signal_bar_close' | 'next_bar_open';
  commissionPerOrder?: number;
}

/**
 * Readable message for a failed POST: FastAPI's string `detail` when the
 * body carries one, otherwise Angular's status line for the response.
 */
function httpErrorMessage(err: unknown): string {
  if (err instanceof HttpErrorResponse) {
    const detail: unknown = err.error?.detail;
    return typeof detail === 'string' ? detail : err.message;
  }
  return err instanceof Error ? err.message : String(err);
}

/**
 * Frontend client for Python's `POST /api/spec-strategy/backtest`.
 *
 * The runner talks to the Python service directly (#1963): the spec goes
 * over the wire as a JSON object and Python's Pydantic schema is the single
 * validator of its shape. Request and response types are the generated
 * OpenAPI contract, so field names are Python's snake_case.
 *
 * The service exposes a tiny signal-based reactive surface alongside
 * the imperative `runBacktest` method:
 *   * `result()`    — last completed backtest result, or null
 *   * `loading()`   — true while a request is in flight
 *   * `error()`     — last error message, or null
 *
 * A backtest that completed with `success: false` still resolves — the
 * result carries Python's `error` text and `error()` mirrors it. A failed
 * HTTP round trip rejects, and `error()` carries a readable message.
 */
@Injectable({ providedIn: 'root' })
export class SpecStrategyService {
  private readonly http = inject(HttpClient);
  private readonly url = `${environment.pythonServiceUrl}/api/spec-strategy/backtest`;

  private readonly _result = signal<SpecStrategyBacktestResult | null>(null);
  private readonly _loading = signal<boolean>(false);
  private readonly _error = signal<string | null>(null);

  readonly result = this._result.asReadonly();
  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();

  async runBacktest(
    spec: StrategySpec,
    options: SpecBacktestRunOptions,
  ): Promise<SpecStrategyBacktestResult> {
    this._loading.set(true);
    this._error.set(null);

    // The optional run params travel only when the caller set them. Python's
    // `SpecBacktestRequest` owns their defaults and applies them to an absent key.
    const body: SpecBacktestRequest = {
      spec,
      start_date: options.startDate,
      end_date: options.endDate,
      ...(options.initialCash !== undefined ? { initial_cash: options.initialCash } : {}),
      ...(options.fillMode !== undefined ? { fill_mode: options.fillMode } : {}),
      ...(options.commissionPerOrder !== undefined
        ? { commission_per_order: options.commissionPerOrder }
        : {}),
    };

    try {
      const result = await firstValueFrom(
        this.http.post<SpecStrategyBacktestResult>(this.url, body),
      );
      this._result.set(result);
      if (!result.success && result.error) {
        this._error.set(result.error);
      }
      return result;
    } catch (err) {
      this._error.set(httpErrorMessage(err));
      throw err;
    } finally {
      this._loading.set(false);
    }
  }

  /** Clear the last result + error. Useful when navigating away. */
  reset(): void {
    this._result.set(null);
    this._error.set(null);
  }
}
