import { Injectable, inject, signal } from '@angular/core';

import { JobsService } from '../../services/jobs.service';
import { DataLakeService, classifyDataLakeError } from '../data-lake';
import { MAX_TRADING_RANGE_DAYS, tradingDateToMs } from '../data-lake';
import type { DataRunSpec, PriceAdjustmentMode } from '../data-lake';
import { BACKFILL_JOB_TYPE } from '../data-lake/backfill-job-type';
import { TickerCatalogService } from '../ticker-catalog';
import { etIsoDate } from '../date/et-midnight';
import { toMostRecentTradingDayIso } from '../date/weekday';

function asString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

/**
 * The adjustment modes the fetch pipeline can actually write. A picker host
 * asking for any other view cannot be served by a backfill, so the gate
 * refuses loudly instead of submitting a spec that changes nothing.
 */
export type BackfillableMode = 'raw' | 'polygon_split_adjusted';

export interface CoverageGateState {
  readonly symbol: string;
  readonly phase: 'backfilling' | 'failed';
  readonly percent: number | null;
  /** Machine reason code — render through `receiptLabel`. */
  readonly reason: string | null;
  /** The backend's own words for a failure, shown as-is. */
  readonly message: string | null;
}

type TerminalResolution =
  | { outcome: 'ready' }
  | { outcome: 'failed'; reason: string; message: string };

/**
 * Turns "the picker offered a symbol the lake doesn't hold" into "the lake
 * holds it now" — the populate-then-use loop behind the shared picker
 * (ADR — symbol picker, 2026-09-20).
 *
 * One backfill at a time, keyed to the operator's latest pick: selecting
 * another symbol supersedes the running gate. The job itself is *not* the
 * gate — cancellation only stops the picker from waiting on it; a job that
 * keeps running server-side is harmless because backfills are additive and
 * the Observatory remains their operator-facing ledger. Completion is
 * verified against the lake catalog (the job said done; the lake is asked
 * whether the bars actually landed) before the caller may proceed.
 */
@Injectable({ providedIn: 'root' })
export class EnsureCoverageService {
  private readonly lake = inject(TickerCatalogService);
  private readonly dataLake = inject(DataLakeService);
  private readonly jobs = inject(JobsService);

  private readonly activeState = signal<CoverageGateState | null>(null);

  /** The gate the picker card renders, if one is open. */
  readonly active = this.activeState.asReadonly();

  private inFlightSymbol: string | null = null;
  private inFlight: Promise<boolean> | null = null;
  private inFlightJobId: string | null = null;
  private unsubscribeEvents: (() => void) | null = null;
  /** Resolves the in-flight gate's promise when the run is abandoned. */
  private inFlightDisarm: (() => void) | null = null;

  /** True when the lake already holds runnable trade bars for `symbol`. */
  isHeld(symbol: string, mode: PriceAdjustmentMode): boolean {
    return this.lake
      .viewFor(mode)
      .pool()
      .some((option) => option.symbol === symbol);
  }

  /**
   * Resolve `symbol` to lake-held bars, backfilling first when needed.
   * Resolves `true` only once the lake answers the membership question
   * affirmatively; `false` leaves {@link active} carrying the failure.
   */
  async ensure(symbol: string, mode: BackfillableMode): Promise<boolean> {
    if (this.isHeld(symbol, mode)) return true;
    if (this.inFlightSymbol === symbol && this.inFlight !== null) {
      return this.inFlight;
    }
    if (this.inFlight !== null) await this.abandonActive();

    this.inFlightSymbol = symbol;
    this.activeState.set({
      symbol,
      phase: 'backfilling',
      percent: null,
      reason: null,
      message: null,
    });
    const run = this.runBackfill(symbol, mode).finally(() => {
      if (this.inFlightSymbol === symbol) {
        this.inFlightSymbol = null;
        this.inFlight = null;
        this.inFlightJobId = null;
      }
    });
    this.inFlight = run;
    return run;
  }

  /**
   * Stop waiting on the active gate. The server job is cancelled best-effort
   * — a refusal to cancel surfaces as a rejected promise to the caller
   * (the card dismisses the strip regardless), and a job that survives
   * simply finishes writing in the background.
   */
  async cancel(): Promise<void> {
    const jobId = this.inFlightJobId;
    this.inFlightSymbol = null;
    this.inFlight = null;
    this.inFlightJobId = null;
    this.activeState.set(null);
    this.closeStream();
    const disarm = this.inFlightDisarm;
    this.inFlightDisarm = null;
    disarm?.();
    if (jobId !== null) await this.jobs.cancelJob(jobId);
  }

  /** Retry the gate's most recent failed symbol. */
  async retry(mode: BackfillableMode): Promise<boolean> {
    const symbol = this.activeState()?.symbol ?? null;
    if (symbol === null) return false;
    return this.ensure(symbol, mode);
  }

  /**
   * Record a refusal the gate cannot act on — an unbackfillable adjustment
   * view, say. The strip renders it through the same failed state as a job
   * failure, so the card has one rendering path for "this pick cannot be
   * covered".
   */
  refuse(symbol: string, reason: string, message: string): void {
    this.fail(symbol, reason, message);
  }

  private async abandonActive(): Promise<void> {
    // A superseded gate's job keeps running server-side — see the class
    // docstring — so only the observation is torn down here. Its promise
    // resolves false rather than hanging on a stream nobody follows.
    this.inFlightSymbol = null;
    this.inFlight = null;
    this.inFlightJobId = null;
    this.activeState.set(null);
    this.closeStream();
    const disarm = this.inFlightDisarm;
    this.inFlightDisarm = null;
    disarm?.();
  }

  private closeStream(): void {
    this.unsubscribeEvents?.();
    this.unsubscribeEvents = null;
  }

  private async runBackfill(symbol: string, mode: BackfillableMode): Promise<boolean> {
    const defaults = await this.dataLake.backfillDefaults();
    if (defaults.kind !== 'ok') {
      return this.fail(symbol, 'backfill_defaults_unavailable', defaults.message);
    }
    const digest = defaults.value.lean_image_digest;
    if (digest === null) {
      return this.fail(
        symbol,
        'backfill_digest_missing',
        'The data plane has no pinned LEAN image digest, so a backfill spec cannot be composed.',
      );
    }

    // Full allowed history, ending at the most recent trading day: the
    // picker's gate does not know the run window a sibling card may later
    // hold, and a maximally covered symbol cannot strand a narrower window.
    // Composed like the Observatory panel's form — checked, not asserted,
    // so a date-helper change fails safe instead of sending NaN.
    const todayIso = etIsoDate(Date.now());
    const startMs = tradingDateToMs(
      toMostRecentTradingDayIso(todayIso, -MAX_TRADING_RANGE_DAYS),
    );
    const endMs = tradingDateToMs(toMostRecentTradingDayIso(todayIso));
    if (startMs === null || endMs === null) {
      return this.fail(
        symbol,
        'backfill_window_invalid',
        'The backfill window could not be composed from the trading calendar.',
      );
    }

    const spec: DataRunSpec = {
      request_id: globalThis.crypto.randomUUID(),
      run_type: 'python_lab',
      market: 'usa',
      symbols: [symbol],
      start_trading_date_ms: startMs,
      end_trading_date_ms: endMs,
      data_types: ['trade'],
      lean_image_digest: digest,
      price_adjustment_mode: mode,
    };

    let jobId: string;
    try {
      jobId = await this.jobs.startJob(BACKFILL_JOB_TYPE, { spec });
    } catch (error) {
      const classified = classifyDataLakeError(error);
      return this.fail(symbol, 'backfill_submission_failed', classified.message);
    }
    this.inFlightJobId = jobId;
    return await this.awaitTerminal(jobId, symbol, mode);
  }

  /** Folds the job's SSE stream to a verdict, mirroring the panel store's fold. */
  private awaitTerminal(
    jobId: string,
    symbol: string,
    mode: BackfillableMode,
  ): Promise<boolean> {
    return new Promise<boolean>((resolve) => {
      const done = (resolution: TerminalResolution) => {
        // A gate cancelled or superseded mid-flight is disarmed: its stream
        // is closed, but a frame already in the pipe must not re-open the
        // strip or settle a promise nobody is waiting on.
        if (this.inFlightJobId !== jobId) {
          resolve(false);
          return;
        }
        this.inFlightDisarm = null;
        this.closeStream();
        void this.settle(symbol, mode, resolution).then(resolve);
      };
      this.inFlightDisarm = () => resolve(false);
      this.unsubscribeEvents = this.jobs.onEvent(jobId, (event) => {
        switch (event.type) {
          case 'job.progress': {
            const current = event['current'];
            const total = event['total'];
            if (typeof current === 'number' && typeof total === 'number' && total > 0) {
              this.activeState.update((state) =>
                state === null || state.symbol !== symbol
                  ? state
                  : { ...state, percent: Math.min(100, Math.round((current / total) * 100)) },
              );
            }
            break;
          }
          case 'job.completed':
            done({ outcome: 'ready' });
            break;
          case 'job.failed':
            done({
              outcome: 'failed',
              reason: asString(event['code']) ?? 'backfill_failed',
              message: asString(event['message']) ?? 'The backfill job failed.',
            });
            break;
          case 'job.cancelled':
            done({
              outcome: 'failed',
              reason: 'backfill_cancelled',
              message: 'The backfill was cancelled.',
            });
            break;
        }
      });
    });
  }

  /**
   * A completed job is not a covered symbol until the lake says so — the
   * reload also refreshes every picker's pool, which is what lets the card
   * read the fresh held span it needs to size the run window.
   */
  private async settle(
    symbol: string,
    mode: BackfillableMode,
    resolution: TerminalResolution,
  ): Promise<boolean> {
    const view = this.lake.viewFor(mode);
    view.reload();
    if (resolution.outcome === 'failed') {
      return this.fail(symbol, resolution.reason, resolution.message);
    }
    if (this.isHeld(symbol, mode)) {
      this.activeState.set(null);
      return true;
    }
    return this.fail(
      symbol,
      'backfill_empty',
      'The backfill finished, but the lake still holds no bars for this symbol.',
    );
  }

  private fail(symbol: string, reason: string, message: string): false {
    this.activeState.set({
      symbol,
      phase: 'failed',
      percent: null,
      reason,
      message,
    });
    return false;
  }
}
