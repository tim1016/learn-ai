import { DestroyRef, computed, inject, resource, signal, type Signal } from '@angular/core';

import { DataLakeService, describeFailure } from '../data-lake';
import type { BackfillJobRun } from '../data-lake/backfill-job-runner';
import { TradeBackfillRunner } from '../data-lake/trade-backfill-runner';
import { TickerCatalogService } from '../ticker-catalog';
import { formatReceiptLabel } from '../pipes/receipt-label.pipe';
import {
  sameHoldingRequest,
  toLakeHolding,
  type LakeCapture,
  type LakeCaptureProgress,
  type LakeHoldingReading,
  type LakeHoldingRequest,
} from './lake-holding';

/** The backfill job's code for a run its owner stopped. */
const CANCELLED_CODE = 'backfill_cancelled';

/** What a lake study's page holds about the lake's holdings: the count, and the one capture that may be running. */
export interface LakeHoldingControl {
  readonly reading: Signal<LakeHoldingReading>;
  readonly capture: Signal<LakeCapture>;
  readonly progress: Signal<LakeCaptureProgress | null>;
  /** Captures the sessions the held count names, through the lake's backfill job. Starts nothing without a count that names some. */
  startCapture(): Promise<void>;
  /** Asks the server to cancel the running capture; what it already wrote stays. */
  cancelCapture(): Promise<void>;
  /** Puts away the note about a capture that stopped. */
  dismissCapture(): void;
  /** Reads the count again, for a page whose own run has just filled the lake. */
  reread(): void;
}

/**
 * The lake-holdings count and capture of one lake study's page (#3521): how many of the sessions the run needs the
 * lake does not hold yet, and the one action that captures them.
 *
 * Call it in a component's field initializer. `request` is the span the run admits, or `null` while the page's inputs
 * are incomplete. The count is read once per distinct request (a change to anything else on the page reads nothing),
 * from catalog rows and never from a bar. Nothing starts a capture but `startCapture`; a run is never blocked by it.
 * The capture is the established backfill job over the span Python named (first through last session it can fetch;
 * sessions already held inside it are reused, not fetched again), the same one the Data tab's Fill submits. When it
 * ends, the count is read again, and the catalog the pickers show is refreshed.
 */
export function createLakeHolding(request: Signal<LakeHoldingRequest | null>): LakeHoldingControl {
  const lake = inject(DataLakeService);
  const runner = inject(TradeBackfillRunner);
  const catalog = inject(TickerCatalogService);

  // Keyed on the request's values: a page that rebuilds an equal request on every keystroke elsewhere reads nothing.
  const wanted = computed(() => request(), { equal: sameHoldingRequest });
  const count = resource({
    params: () => wanted() ?? undefined,
    loader: async ({ params }): Promise<LakeHoldingReading> => {
      const read = await lake.instrumentData({
        symbol: params.symbol,
        startTradingDate: params.from,
        endTradingDate: params.to,
        priceAdjustmentMode: params.tree,
      });
      if (read.kind === 'ok') return { kind: 'read', request: params, holding: toLakeHolding(read.value) };
      const problem = describeFailure(read);
      return { kind: 'unreadable', message: `${formatReceiptLabel(problem?.reason ?? 'unavailable')}: ${problem?.message ?? read.message}` };
    },
  });

  const reading = computed<LakeHoldingReading>(() => {
    switch (count.status()) {
      case 'idle':
        return { kind: 'idle' };
      case 'error':
        return { kind: 'unreadable', message: count.error()?.message ?? 'The lake could not be read.' };
      default:
        return count.value() ?? { kind: 'checking' };
    }
  });

  const capture = signal<LakeCapture>({ kind: 'idle' });
  const job = signal<BackfillJobRun | null>(null);
  let destroyed = false;
  inject(DestroyRef).onDestroy(() => {
    destroyed = true;
    // Leaving the page stops watching, never the job: it goes on writing, as the picker's gate leaves it.
    job()?.detach();
  });

  return {
    reading,
    capture: capture.asReadonly(),
    progress: computed(() => {
      const tick = job()?.state().progress ?? null;
      return tick === null ? null : { done: tick.current, total: tick.total };
    }),

    async startCapture() {
      const held = reading();
      if (held.kind !== 'read' || capture().kind === 'running') return;
      const { request: counted, holding } = held;
      const span = holding.captureSpan;
      if (span === null) return;
      capture.set({ kind: 'running', symbol: counted.symbol, sessions: holding.toCapture });

      const end = await runner.run(
        () => ({
          symbol: counted.symbol,
          price_adjustment_mode: counted.tree,
          start_trading_date_ms: span.startMs,
          end_trading_date_ms: span.endMs,
        }),
        (accepted) => {
          if (destroyed) return false;
          job.set(accepted);
          return true;
        },
      );
      job.set(null);
      if (destroyed) return;
      if (end.kind === 'detached') {
        capture.set({ kind: 'idle' });
        return;
      }
      // The pickers' held spans are read off the lake, which the job wrote to, cancelled or not.
      catalog.viewFor(counted.tree).reload();
      capture.set(
        end.kind === 'completed'
          ? { kind: 'idle' }
          : { kind: 'stopped', cancelled: end.code === CANCELLED_CODE, message: `${formatReceiptLabel(end.code)}: ${end.message}` },
      );
      count.reload();
    },

    async cancelCapture() {
      try {
        await job()?.cancel();
      } catch (error) {
        // The page must not keep saying "capturing" for a job it could not stop; the job may still be writing.
        const why = error instanceof Error ? error.message : String(error);
        if (capture().kind === 'running') {
          capture.set({ kind: 'stopped', cancelled: false, message: `The server could not be told to stop it, so it may still be running. ${why}` });
        }
      }
    },

    dismissCapture() {
      if (capture().kind === 'stopped') capture.set({ kind: 'idle' });
    },

    reread() {
      count.reload();
    },
  };
}
