import { Injectable, inject, signal } from '@angular/core';

import { JobsService } from '../../services/jobs.service';
import { DataLakeService, classifyDataLakeError } from '../data-lake';
import { tradingDateToMs, tradingRangeRejection, tradingRangeSpanDays } from '../data-lake';
import type { DataRunSpec, PriceAdjustmentMode } from '../data-lake';
import { BACKFILL_JOB_TYPE } from '../data-lake/backfill-job-type';
import { TickerCatalogService } from '../ticker-catalog';
import { etIsoDate, isoDateAfter } from '../date/et-midnight';
import { toMostRecentTradingDayIso } from '../date/weekday';

function asString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

/**
 * The widest backfill window that fits the data plane's **inclusive** range
 * cap, ending at the most recent trading day on or before `todayIso`.
 *
 * A naive `today - cap` start is wrong twice over: the cap counts the window
 * inclusively (`(end - start).days + 1`), and the weekday walk-back on the
 * endpoints can add up to three more days (1830 is not a whole number of
 * weeks), which is how a window composed on a Monday lands on a 422 while
 * the same code passes on a Saturday. The start therefore begins four days
 * inside the cap and steps forward — a step that keeps both endpoints on
 * weekdays — until the inclusive span fits, and the caller still runs the
 * result through `tradingRangeRejection` so a cap the data plane lowered
 * fails loudly instead of shipping an oversized spec.
 */
export function fitBackfillWindow(
  todayIso: string,
  capDays: number,
): { start: string; end: string } {
  const end = toMostRecentTradingDayIso(todayIso);
  let start = toMostRecentTradingDayIso(end, -(capDays - 4));
  let guard = 0;
  while (
    guard <= 7 &&
    (tradingRangeSpanDays(start, end) ?? capDays + 1) > capDays
  ) {
    start = isoDateAfter(start);
    guard++;
  }
  return { start, end };
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
 * The one in-flight gate. It used to be five loose fields, and a cancel
 * landing while `startJob` was still in flight fell through all of them —
 * the job id was written back after the cancel, and the dismissed strip
 * re-opened. Every late asynchronous continuation now carries the entry's
 * token and re-checks `live(token)` before it may touch state.
 */
interface ActiveRun {
  readonly token: number;
  readonly symbol: string;
  run: Promise<boolean>;
  /** The job's id once the server accepted it — `null` while submitting. */
  jobId: string | null;
  unsubscribe: (() => void) | null;
  /** Resolves the run `false` when the gate is torn down mid-flight. */
  disarm: (() => void) | null;
}

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

  private current: ActiveRun | null = null;
  private nextToken = 1;

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
    if (this.current?.symbol === symbol) return this.current.run;
    this.abandonActive();

    const token = this.nextToken++;
    this.activeState.set({
      symbol,
      phase: 'backfilling',
      percent: null,
      reason: null,
      message: null,
    });
    const entry: ActiveRun = {
      token,
      symbol,
      run: null!,
      jobId: null,
      unsubscribe: null,
      disarm: null,
    };
    this.current = entry;
    const run = this.runBackfill(symbol, mode, entry);
    entry.run = run;
    // A settled run — covered, failed, cancelled or superseded — releases
    // the gate slot immediately, so the same symbol is freely re-ensurable
    // while the failed strip it left behind stays on screen until retry or
    // dismissal.
    void run.then(() => {
      if (this.current === entry) this.current = null;
    });
    return run;
  }

  /**
   * Tear the gate down on the operator's behalf: stop observing, resolve
   * the waiter, and cancel the server job once its id is known. A refusal
   * to cancel propagates to the caller; a job that survives simply
   * finishes writing in the background.
   */
  async cancel(): Promise<void> {
    const entry = this.current;
    this.current = null;
    this.activeState.set(null);
    if (entry === null) return;
    entry.unsubscribe?.();
    entry.disarm?.();
    if (entry.jobId !== null) await this.jobs.cancelJob(entry.jobId);
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
    this.abandonActive();
    this.activeState.set({
      symbol,
      phase: 'failed',
      percent: null,
      reason,
      message,
    });
  }

  /**
   * The superseded gate's job keeps running server-side — see the class
   * docstring — so only the observation is torn down here.
   */
  private abandonActive(): void {
    const entry = this.current;
    this.current = null;
    if (entry === null) return;
    this.activeState.set(null);
    entry.unsubscribe?.();
    entry.disarm?.();
  }

  private live(token: number): boolean {
    return this.current?.token === token;
  }

  private async runBackfill(
    symbol: string,
    mode: BackfillableMode,
    entry: ActiveRun,
  ): Promise<boolean> {
    const defaults = await this.dataLake.backfillDefaults();
    if (!this.live(entry.token)) return false;
    if (defaults.kind !== 'ok') {
      return this.fail(entry, 'backfill_defaults_unavailable', defaults.message);
    }
    const digest = defaults.value.lean_image_digest;
    if (digest === null) {
      return this.fail(
        entry,
        'backfill_digest_missing',
        'The data plane has no pinned LEAN image digest, so a backfill spec cannot be composed.',
      );
    }

    // Full allowed history, ending at the most recent trading day: the
    // picker's gate does not know the run window a sibling card may later
    // hold, and a maximally covered symbol cannot strand a narrower window.
    const todayIso = etIsoDate(Date.now());
    const { start, end } = fitBackfillWindow(todayIso, defaults.value.max_trading_range_days);
    const rejection = tradingRangeRejection(
      start,
      end,
      defaults.value.max_trading_range_days,
    );
    const startMs = tradingDateToMs(start);
    const endMs = tradingDateToMs(end);
    if (rejection !== null || startMs === null || endMs === null) {
      return this.fail(
        entry,
        'backfill_window_invalid',
        rejection ?? 'The backfill window could not be composed from the trading calendar.',
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
      return this.fail(entry, 'backfill_submission_failed', classified.message);
    }
    // The operator cancelled or moved on while submission was in flight:
    // the job id only exists now, so stop the just-accepted job here.
    if (!this.live(entry.token)) {
      await this.jobs.cancelJob(jobId);
      return false;
    }
    entry.jobId = jobId;
    return await this.awaitTerminal(jobId, symbol, mode, entry);
  }

  /** Folds the job's SSE stream to a verdict, mirroring the panel store's fold. */
  private awaitTerminal(
    jobId: string,
    symbol: string,
    mode: BackfillableMode,
    entry: ActiveRun,
  ): Promise<boolean> {
    return new Promise<boolean>((resolve) => {
      const done = (resolution: TerminalResolution) => {
        if (!this.live(entry.token)) {
          resolve(false);
          return;
        }
        entry.unsubscribe?.();
        void this.settle(symbol, mode, resolution, entry).then(resolve);
      };
      entry.disarm = () => resolve(false);
      entry.unsubscribe = this.jobs.onEvent(jobId, (event) => {
        if (!this.live(entry.token)) return;
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
    entry: ActiveRun,
  ): Promise<boolean> {
    const view = this.lake.viewFor(mode);
    view.reload();
    if (resolution.outcome === 'failed') {
      return this.fail(entry, resolution.reason, resolution.message);
    }
    if (this.isHeld(symbol, mode)) {
      this.activeState.set(null);
      return true;
    }
    return this.fail(
      entry,
      'backfill_empty',
      'The backfill finished, but the lake still holds no bars for this symbol.',
    );
  }

  private fail(entry: ActiveRun, reason: string, message: string): false {
    if (this.live(entry.token)) {
      this.activeState.set({
        symbol: entry.symbol,
        phase: 'failed',
        percent: null,
        reason,
        message,
      });
    }
    return false;
  }
}
