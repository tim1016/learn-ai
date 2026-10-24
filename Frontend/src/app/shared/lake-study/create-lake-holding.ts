import { computed, inject, resource, signal, type Signal } from '@angular/core';

import { DataLakeService, describeFailure, sameInstrumentDataRequest, type InstrumentDataRequest } from '../data-lake';
import { createFillWindowCapture, type CaptureProgress } from '../data-lake/capture-fill-window';
import { formatReceiptLabel } from '../pipes/receipt-label.pipe';
import { toLakeHolding, type LakeCapture, type LakeHoldingReading } from './lake-holding';

/** What a lake study's page holds about the lake's holdings: the count, and the one capture that may be running. */
export interface LakeHoldingControl {
  readonly reading: Signal<LakeHoldingReading>;
  readonly capture: Signal<LakeCapture>;
  readonly progress: Signal<CaptureProgress | null>;
  /** Captures the sessions the held count names, through the lake's backfill job. Starts nothing without a count that names some, or while a capture is in flight. */
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
 * Call it in a component's field initializer. `request` is the window the run is given, or `null` while the page's
 * inputs are incomplete. The count is read once per distinct request (a change to anything else on the page reads
 * nothing), from catalog rows and never from a bar. Nothing starts a capture but `startCapture`; a run is never
 * blocked by it. The capture is the Data tab's Fill (`createFillWindowCapture`) over the span Python named; when it
 * ends, the count is read again.
 */
export function createLakeHolding(request: Signal<InstrumentDataRequest | null>): LakeHoldingControl {
  const lake = inject(DataLakeService);
  const fill = createFillWindowCapture();

  // Keyed on the request's values: a page that rebuilds an equal request on every keystroke elsewhere reads nothing.
  const wanted = computed(() => request(), { equal: sameInstrumentDataRequest });
  const count = resource({
    params: () => wanted() ?? undefined,
    loader: async ({ params }): Promise<LakeHoldingReading> => {
      const read = await lake.instrumentData(params);
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

  return {
    reading,
    capture: capture.asReadonly(),
    progress: fill.progress,

    async startCapture() {
      const held = reading();
      // `busy` outlasts the note: after a cancel the server refused, the job may still be writing.
      if (held.kind !== 'read' || fill.busy()) return;
      const { request: counted, holding } = held;
      const span = holding.captureSpan;
      if (span === null) return;
      capture.set({ kind: 'running', symbol: counted.symbol, sessions: holding.toCapture });

      const end = await fill.run(counted, span);
      switch (end.kind) {
        case 'detached':
          capture.set({ kind: 'idle' });
          return;
        case 'completed':
          capture.set({ kind: 'idle' });
          break;
        case 'cancelled':
          capture.set({ kind: 'stopped', cancelled: true, message: end.message });
          break;
        case 'failed':
          capture.set({ kind: 'stopped', cancelled: false, message: `${formatReceiptLabel(end.code)}: ${end.message}` });
          break;
      }
      count.reload();
    },

    async cancelCapture() {
      const refused = await fill.cancel();
      // The page must not keep saying "capturing" for a job it could not stop; the job may still be writing.
      if (refused !== null && capture().kind === 'running') capture.set({ kind: 'stopped', cancelled: false, message: refused });
    },

    dismissCapture() {
      if (capture().kind === 'stopped') capture.set({ kind: 'idle' });
    },

    reread() {
      count.reload();
    },
  };
}
