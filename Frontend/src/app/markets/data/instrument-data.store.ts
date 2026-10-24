import { DestroyRef, Injectable, computed, inject, signal } from '@angular/core';

import { DataLabWorkspaceStore } from '../../components/data-lab/data-lab-workspace-store';
import { DataLakeService, describeFailure, type InstrumentDataReport } from '../../shared/data-lake';
import type { BackfillJobRun } from '../../shared/data-lake/backfill-job-runner';
import { TradeBackfillRunner } from '../../shared/data-lake/trade-backfill-runner';
import { ReturnsStudyHold } from '../returns/returns-study-hold';
import { fillReport, type AdjustmentTree, type FillEnd, type FillReport } from './instrument-data';

/** What Python is asked about: one instrument's whole-day minute bars over a window, in one lake tree. */
export interface InspectionRequest {
  readonly symbol: string;
  /** `YYYY-MM-DD`, both included: the committed window as the date picker holds it. */
  readonly from: string;
  readonly to: string;
  readonly tree: AdjustmentTree;
}

export type Inspection =
  | { readonly kind: 'idle' }
  | { readonly kind: 'checking'; readonly request: InspectionRequest }
  | { readonly kind: 'read'; readonly request: InspectionRequest; readonly report: InstrumentDataReport }
  /** Python refused the request and said why; nothing is substituted for it. */
  | { readonly kind: 'refused'; readonly request: InspectionRequest; readonly reason: string; readonly message: string }
  | { readonly kind: 'unavailable'; readonly request: InspectionRequest; readonly message: string };

/** How far a running Fill's job has got, in the sessions the server counted. */
export interface FillProgress {
  readonly done: number;
  readonly total: number;
}

export type FillState =
  | { readonly kind: 'idle' }
  | { readonly kind: 'running'; readonly request: InspectionRequest; readonly sessions: number }
  | {
      readonly kind: 'ended';
      readonly request: InspectionRequest;
      readonly end: FillEnd;
      /** The lake's reading after the job, set against the one before it. `null` when it could not be read again. */
      readonly report: FillReport | null;
      readonly rereadProblem: string | null;
    };

function sameRequest(a: InspectionRequest, b: InspectionRequest): boolean {
  return a.symbol === b.symbol && a.from === b.from && a.to === b.to && a.tree === b.tree;
}

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
  private readonly runner = inject(TradeBackfillRunner);
  private readonly workspace = inject(DataLabWorkspaceStore);
  private readonly returnsHold = inject(ReturnsStudyHold);

  private readonly inspectionState = signal<Inspection>({ kind: 'idle' });
  private readonly fillState = signal<FillState>({ kind: 'idle' });
  private readonly jobState = signal<BackfillJobRun | null>(null);
  /** Counts the readings asked for; an answer applies only while it is the latest. */
  private turn = 0;
  private destroyed = false;

  readonly inspection = this.inspectionState.asReadonly();
  readonly fill = this.fillState.asReadonly();
  readonly filling = computed(() => this.fillState().kind === 'running');
  /** The running job's own progress, from the server's ticks. */
  readonly progress = computed<FillProgress | null>(() => {
    const state = this.jobState()?.state();
    const tick = state?.progress ?? null;
    return tick === null ? null : { done: tick.current, total: tick.total };
  });

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      this.destroyed = true;
      // Leaving the place stops watching, never the job: it goes on writing, as the picker's gate leaves it.
      this.jobState()?.detach();
    });
  }

  /** Reads the request unless that is what this place already holds or is already reading. */
  show(request: InspectionRequest): void {
    const held = this.inspectionState();
    if (held.kind !== 'idle' && sameRequest(held.request, request) && held.kind !== 'unavailable') return;
    void this.read(request);
  }

  /** Reads the request again, whatever is held: the explicit Check. */
  check(request: InspectionRequest): void {
    void this.read(request);
  }

  private async read(request: InspectionRequest): Promise<void> {
    const turn = ++this.turn;
    this.inspectionState.set({ kind: 'checking', request });
    const reading = await this.lake.instrumentData({
      symbol: request.symbol,
      startTradingDate: request.from,
      endTradingDate: request.to,
      priceAdjustmentMode: request.tree,
    });
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
    if (held.kind !== 'read' || this.fillState().kind === 'running') return;
    const { request, report: before } = held;
    const window = before.fill_window;
    if (window === null || window === undefined) return;
    this.fillState.set({ kind: 'running', request, sessions: before.summary.fetchable });
    // Not a new turn: a reading of any request started while this Fill runs keeps the screen.
    const turn = this.turn;

    const outcome = await this.runner.run(
      () => ({
        symbol: request.symbol,
        price_adjustment_mode: request.tree,
        start_trading_date_ms: window.start_trading_date_ms,
        end_trading_date_ms: window.end_trading_date_ms,
      }),
      (job) => {
        if (this.destroyed) return false;
        this.jobState.set(job);
        return true;
      },
    );
    this.jobState.set(null);
    if (this.destroyed || outcome.kind === 'detached') {
      if (!this.destroyed) this.fillState.set({ kind: 'idle' });
      return;
    }
    const end: FillEnd = outcome.kind === 'completed' ? { kind: 'completed' } : outcome;

    const reading = await this.lake.instrumentData({
      symbol: request.symbol,
      startTradingDate: request.from,
      endTradingDate: request.to,
      priceAdjustmentMode: request.tree,
    });
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

  /** Asks the server to cancel the running job; what it had written stays, and the report says so. */
  async cancelFill(): Promise<void> {
    await this.jobState()?.cancel();
  }

  /** Puts the last Fill's report away. */
  dismissFill(): void {
    if (this.fillState().kind === 'ended') this.fillState.set({ kind: 'idle' });
  }
}
