import { DestroyRef, computed, inject, signal, type Signal } from '@angular/core';

import { TickerCatalogService } from '../ticker-catalog';
import type { BackfillJobRun } from './backfill-job-runner';
import type { InstrumentDataReport } from './data-lake.service';
import type { InstrumentDataRequest } from './instrument-data-request';
import { TradeBackfillRunner, type TradeBackfillEnd } from './trade-backfill-runner';

/** The span the server names for a capture: first through last session it can fetch (`int64 ms UTC`, as the backfill takes it). */
export type FillWindow = NonNullable<InstrumentDataReport['fill_window']>;

/** How far a running capture's job has got, in the days of its span that the server counted. */
export interface CaptureProgress {
  readonly done: number;
  readonly total: number;
}

/** The one capture of the server's `fill_window` that a page may have running. */
export interface FillWindowCapture {
  /** A capture is in flight: from `run` to its end, including a job whose cancel the server refused, which may still be writing. */
  readonly busy: Signal<boolean>;
  /** The running job's own progress, from the server's ticks. */
  readonly progress: Signal<CaptureProgress | null>;
  /**
   * Captures `window`, the span the server's reading named, for one instrument in one tree, through the established
   * backfill job (sessions already held inside it are reused, not fetched again). Call it only while `busy` is false.
   * When the job has ended, the catalog the pickers show is read again, because the lake was written to whether the
   * job completed, failed or was cancelled. `detached` means this page stopped watching: the job may still be running.
   */
  run(subject: Pick<InstrumentDataRequest, 'symbol' | 'tree'>, window: FillWindow): Promise<TradeBackfillEnd>;
  /** Asks the server to cancel the running job; what it already wrote stays. Resolves with why it could not, or `null`. */
  cancel(): Promise<string | null>;
}

/**
 * The capture a lake page offers for the gaps the server's instrument-data reading names (#3521): the Data tab's Fill
 * and a lake study's "Capture", one flow. Call it in an injection context (a component or store field initializer).
 *
 * Leaving the page stops watching the job and never stops it: it goes on writing, as the picker's gate leaves it.
 */
export function createFillWindowCapture(): FillWindowCapture {
  const runner = inject(TradeBackfillRunner);
  const catalog = inject(TickerCatalogService);

  const job = signal<BackfillJobRun | null>(null);
  const busy = signal(false);
  let left = false;
  inject(DestroyRef).onDestroy(() => {
    left = true;
    job()?.detach();
  });

  return {
    busy: busy.asReadonly(),
    progress: computed(() => {
      const tick = job()?.state().progress ?? null;
      return tick === null ? null : { done: tick.current, total: tick.total };
    }),

    async run({ symbol, tree }, window) {
      busy.set(true);
      try {
        const end = await runner.run(
          () => ({
            symbol,
            price_adjustment_mode: tree,
            start_trading_date_ms: window.start_trading_date_ms,
            end_trading_date_ms: window.end_trading_date_ms,
          }),
          (accepted) => {
            if (left) {
              accepted.detach();
              return false;
            }
            job.set(accepted);
            return true;
          },
        );
        job.set(null);
        if (left) return { kind: 'detached' };
        // The pickers' held spans are read off the lake, which the job wrote to, cancelled or not.
        if (end.kind !== 'detached') catalog.viewFor(tree).reload();
        return end;
      } finally {
        busy.set(false);
      }
    },

    async cancel() {
      try {
        await job()?.cancel();
        return null;
      } catch (error) {
        const why = error instanceof Error ? error.message : String(error);
        return `The server could not be told to stop it, so it may still be running. ${why}`;
      }
    },
  };
}
