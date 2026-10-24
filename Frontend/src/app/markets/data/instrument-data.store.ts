import { DestroyRef, Injectable, computed, inject, signal } from '@angular/core';

import { DataLabWorkspaceStore } from '../../components/data-lab/data-lab-workspace-store';
import {
  DataLakeService,
  describeFailure,
  sameInstrumentDataRequest,
  type InstrumentDataReport,
  type InstrumentDataRequest,
} from '../../shared/data-lake';
import { createFillWindowCapture } from '../../shared/data-lake/capture-fill-window';
import { ReturnsStudyHold } from '../returns/returns-study-hold';
import { fillReport, type FillEnd, type FillReport } from './instrument-data';

export type Inspection =
  | { readonly kind: 'idle' }
  | { readonly kind: 'checking'; readonly request: InstrumentDataRequest }
  | { readonly kind: 'read'; readonly request: InstrumentDataRequest; readonly report: InstrumentDataReport }
  /** Python refused the request and said why; nothing is substituted for it. */
  | { readonly kind: 'refused'; readonly request: InstrumentDataRequest; readonly reason: string; readonly message: string }
  | { readonly kind: 'unavailable'; readonly request: InstrumentDataRequest; readonly message: string };

export type FillState =
  | { readonly kind: 'idle' }
  | {
      readonly kind: 'running';
      readonly request: InstrumentDataRequest;
      readonly sessions: number;
      /** Why the server could not be told to stop the job, when it refused a cancel; the job may still be writing. */
      readonly cancelRefused: string | null;
    }
  | {
      readonly kind: 'ended';
      readonly request: InstrumentDataRequest;
      readonly end: FillEnd;
      /** The lake's reading after the job, set against the one before it. `null` when it could not be read again. */
      readonly report: FillReport | null;
      readonly rereadProblem: string | null;
    };

/**
 * What an instrument's place knows about its data: Python's last reading of the
 * committed request, and the one Fill that may be running.
 *
 * Provided by the place, so it outlives a tab change. Coming back to the Data
 * tab with the request unchanged shows the reading it holds and reads nothing;
 * a different request reads once, and a reading that a newer request replaced
 * before it came back is dropped, so a late answer never fills a context it was
 * not asked for. Reading is read-only on the server. A Fill is the one explicit
 * action that writes: it submits the established backfill job over the window
 * Python named, one at a time, then reads the lake again and reports the
 * difference. A session another writer completes meanwhile is counted as added
 * to this window, which is what the window now holds.
 */
@Injectable()
export class InstrumentDataStore {
  private readonly lake = inject(DataLakeService);
  private readonly capture = createFillWindowCapture();
  private readonly workspace = inject(DataLabWorkspaceStore);
  private readonly returnsHold = inject(ReturnsStudyHold);

  private readonly inspectionState = signal<Inspection>({ kind: 'idle' });
  private readonly fillState = signal<FillState>({ kind: 'idle' });
  /** Counts the readings asked for; an answer applies only while it is the latest. */
  private turn = 0;
  private destroyed = false;

  readonly inspection = this.inspectionState.asReadonly();
  readonly fill = this.fillState.asReadonly();
  readonly filling = computed(() => this.fillState().kind === 'running');
  /** The running job's own progress, from the server's ticks. */
  readonly progress = this.capture.progress;

  constructor() {
    // Leaving the place stops watching the job, never the job (the capture does that); a late answer is dropped.
    inject(DestroyRef).onDestroy(() => {
      this.destroyed = true;
    });
  }

  /** Reads the request unless that is what this place already holds or is already reading. */
  show(request: InstrumentDataRequest): void {
    const held = this.inspectionState();
    if (held.kind !== 'idle' && sameInstrumentDataRequest(held.request, request) && held.kind !== 'unavailable') return;
    void this.read(request);
  }

  /** Reads the request again, whatever is held: the explicit Check. */
  check(request: InstrumentDataRequest): void {
    void this.read(request);
  }

  private async read(request: InstrumentDataRequest): Promise<void> {
    const turn = ++this.turn;
    this.inspectionState.set({ kind: 'checking', request });
    const reading = await this.lake.instrumentData(request);
    if (turn !== this.turn || this.destroyed) return;
    if (reading.kind === 'ok') {
      this.inspectionState.set({ kind: 'read', request, report: reading.value });
      return;
    }
    const problem = describeFailure(reading);
    this.inspectionState.set(
      reading.kind === 'rejected'
        ? { kind: 'refused', request, reason: reading.reason, message: reading.message }
        : { kind: 'unavailable', request, message: problem?.message ?? reading.message },
    );
  }

  /**
   * Fills the gaps the held reading names, one Fill at a time.
   *
   * It asks only for the window Python gave (first through last fetchable
   * session), never one the browser composed, and does nothing when the reading
   * names none, so a second press while one runs, or a press on a window
   * with nothing to fetch, submits nothing.
   */
  async startFill(): Promise<void> {
    const held = this.inspectionState();
    // A Fill stays `running` until its job has ended, a cancel the server refused included, so this one guard holds.
    if (held.kind !== 'read' || this.fillState().kind === 'running') return;
    const { request, report: before } = held;
    const window = before.fill_window;
    if (window === null || window === undefined) return;
    this.fillState.set({ kind: 'running', request, sessions: before.summary.fetchable, cancelRefused: null });
    // Not a new turn: a reading of any request started while this Fill runs keeps the screen.
    const turn = this.turn;

    const outcome = await this.capture.run(request, window);
    if (outcome.kind === 'detached') {
      if (!this.destroyed) this.fillState.set({ kind: 'idle' });
      return;
    }
    const end: FillEnd = outcome.kind === 'completed' ? { kind: 'completed' } : outcome;

    const reading = await this.lake.instrumentData(request);
    if (this.destroyed) return;
    if (reading.kind !== 'ok') {
      this.fillState.set({
        kind: 'ended',
        request,
        end,
        report: null,
        rereadProblem: describeFailure(reading)?.message ?? 'The lake could not be read again.',
      });
      return;
    }
    const report = fillReport(before, reading.value, end);
    // What a Fill wrote is new evidence: a study or a chart held from before it no longer answers the lake.
    if (report.added > 0) {
      this.returnsHold.invalidate(request.symbol);
      this.workspace.markChartStale();
    }
    // The newest request owns the screen: a Fill that ended after the person moved on reports, and shows nothing else.
    if (turn === this.turn) this.inspectionState.set({ kind: 'read', request, report: reading.value });
    this.fillState.set({ kind: 'ended', request, end, report, rereadProblem: null });
  }

  /**
   * Asks the server to cancel the running job; what it had written stays, and the report says so. A server that
   * could not be told is said so beside the running Fill, which is still what the job is doing.
   */
  async cancelFill(): Promise<void> {
    const refused = await this.capture.cancel();
    if (refused === null) return;
    this.fillState.update((state) => (state.kind === 'running' ? { ...state, cancelRefused: refused } : state));
  }

  /** Puts the last Fill's report away. */
  dismissFill(): void {
    if (this.fillState().kind === 'ended') this.fillState.set({ kind: 'idle' });
  }
}
