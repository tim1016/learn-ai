import { Injectable, inject, signal } from '@angular/core';
import { Apollo, gql } from 'apollo-angular';
import { firstValueFrom } from 'rxjs';
import {
  RunSpecStrategyBacktestResponse,
  RunSpecStrategyBacktestVariables,
  SpecStrategyBacktestResult,
  StrategySpec,
} from '../graphql/spec-strategy-types';

const RUN_SPEC_STRATEGY_BACKTEST = gql`
  mutation RunSpecStrategyBacktest(
    $specJson: String!
    $startDate: String!
    $endDate: String!
    $initialCash: Decimal
    $fillMode: String
    $commissionPerOrder: Decimal
  ) {
    runSpecStrategyBacktest(
      specJson: $specJson
      startDate: $startDate
      endDate: $endDate
      initialCash: $initialCash
      fillMode: $fillMode
      commissionPerOrder: $commissionPerOrder
    ) {
      success
      strategyName
      initialCash
      finalEquity
      netProfit
      totalFees
      totalTrades
      winningTrades
      losingTrades
      winRate
      trades {
        tradeNumber
        entryTime
        entryPrice
        exitTime
        exitPrice
        indicators
        pnlPts
        pnlPct
        result
        signalReason
      }
      logLines
      error
    }
  }
`;

/**
 * Frontend wrapper around the `runSpecStrategyBacktest` GraphQL mutation.
 *
 * Apollo round-trips the spec as a JSON string (the mutation declares
 * `specJson: String!`) — caller passes a typed `StrategySpec`, the
 * service handles serialization. Backend deserializes the string into a
 * JsonNode and re-emits it as a JSON object on the wire to the Python
 * service, so the Python Pydantic schema sees a proper object as
 * required.
 *
 * The service exposes a tiny signal-based reactive surface alongside
 * the imperative `runBacktest` method:
 *   * `result()`    — last completed backtest result, or null
 *   * `loading()`   — true while a request is in flight
 *   * `error()`     — last error message, or null
 *
 * UI components can either call `runBacktest()` and await the result
 * directly, or read the signals to drive a reactive view. Phase 1 of
 * the UI work uses the imperative path; signals are there for the
 * eventual editor where multiple components share one in-flight run.
 */
@Injectable({ providedIn: 'root' })
export class SpecStrategyService {
  private readonly apollo = inject(Apollo);

  private readonly _result = signal<SpecStrategyBacktestResult | null>(null);
  private readonly _loading = signal<boolean>(false);
  private readonly _error = signal<string | null>(null);

  readonly result = this._result.asReadonly();
  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();

  /**
   * Run a backtest with a fully-typed `StrategySpec`.
   *
   * The spec is serialized via `JSON.stringify` and the resulting
   * string is sent as the `specJson` mutation variable. Backend parses
   * it back into a JSON object before forwarding to Python so the
   * Pydantic schema sees the structural form.
   */
  async runBacktest(
    spec: StrategySpec,
    options: {
      startDate: string;
      endDate: string;
      initialCash?: number;
      fillMode?: 'signal_bar_close' | 'next_bar_open';
      commissionPerOrder?: number;
    },
  ): Promise<SpecStrategyBacktestResult> {
    this._loading.set(true);
    this._error.set(null);

    const variables: RunSpecStrategyBacktestVariables = {
      specJson: JSON.stringify(spec),
      startDate: options.startDate,
      endDate: options.endDate,
      initialCash: options.initialCash,
      fillMode: options.fillMode,
      commissionPerOrder: options.commissionPerOrder,
    };

    try {
      const response = await firstValueFrom(
        this.apollo.mutate<RunSpecStrategyBacktestResponse, RunSpecStrategyBacktestVariables>({
          mutation: RUN_SPEC_STRATEGY_BACKTEST,
          variables,
        }),
      );

      const result = response.data?.runSpecStrategyBacktest;
      if (!result) {
        throw new Error('No data returned from runSpecStrategyBacktest');
      }
      this._result.set(result);
      if (!result.success && result.error) {
        this._error.set(result.error);
      }
      return result;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this._error.set(message);
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

export { RUN_SPEC_STRATEGY_BACKTEST };
