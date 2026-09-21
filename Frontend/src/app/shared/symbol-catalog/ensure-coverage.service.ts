import { Injectable, computed, inject, signal, type Signal } from '@angular/core';

import { JobsService } from '../../services/jobs.service';
import { DataLakeService } from '../data-lake';
import {
  BackfillJobRunner,
  BackfillSubmissionError,
  type BackfillJobRun,
} from '../data-lake/backfill-job-runner';
import { tradingDateToMs, tradingRangeRejection, tradingRangeSpanDays } from '../data-lake';
import type { DataRunSpec, PriceAdjustmentMode } from '../data-lake';
import { isRunnableSpan } from '../ticker-catalog';
import { TickerCatalogService } from '../ticker-catalog';
import { etIsoDate, isoDateAfter } from '../date/et-midnight';
import { toMostRecentTradingDayIso } from '../date/weekday';

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
  /** The lake tree this gate fills — part of the gate's identity (ADR 0066). */
  readonly mode: PriceAdjustmentMode;
  readonly phase: 'backfilling' | 'failed';
  readonly percent: number | null;
  /** Machine reason code — render through `receiptLabel`. */
  readonly reason: string | null;
  /** The backend's own words for a failure, shown as-is. */
  readonly message: string | null;
}

/**
 * One caller's handle on a coverage gate — opaque on purpose.
 *
 * `ensure()` may coalesce two cards' identical requests onto one running
 * backfill, so no caller may infer ownership from the gate's *fields*
 * (symbol, mode): two cards can legitimately wait on the same run, and a
 * card that inspects `state.symbol` cannot tell "my gate" from "a gate that
 * happens to be about the same symbol in another tree". The handle is the
 * ownership: the strip renders `session.state`, completion is awaited on
 * `session.done`, and `session.cancel()` releases only this session — the
 * underlying job is stopped when the last attached session goes away, never
 * out from under a co-waiting card.
 */
export interface CoverageGateSession {
  /** This session's gate strip, `null` once detached or nothing is owed. */
  readonly state: Signal<CoverageGateState | null>;
  /** Resolves `true` only when the lake confirmed the bars for this pick. */
  readonly done: Promise<boolean>;
  /** Detach this session; stops the job only if no other session waits. */
  cancel(): Promise<void>;
}

/**
 * Turns "the picker offered a symbol the lake doesn't hold" into "the lake
 * holds it now" — the populate-then-use loop behind the shared picker
 * (ADR 0066).
 *
 * One backfill at a time, keyed to the operator's latest pick: selecting
 * another symbol (or another adjustment tree) supersedes the running gate.
 * Submission, the SSE fold and terminal classification are not duplicated
 * here — this service composes `BackfillJobRunner` and adds only the
 * coverage question: completion is verified against a **fresh** lake read
 * (the job said done; the lake is asked whether the bars actually landed)
 * before any session may proceed. A superseded job keeps running
 * server-side — backfills are additive and the Observatory remains their
 * operator-facing ledger.
 */
@Injectable({ providedIn: 'root' })
export class EnsureCoverageService {
  private readonly lake = inject(TickerCatalogService);
  private readonly dataLake = inject(DataLakeService);
  private readonly jobs = inject(JobsService);
  private readonly runner = inject(BackfillJobRunner);

  private readonly activeState = signal<CoverageGateState | null>(null);

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
   * The session's `done` resolves `true` only once the lake answers the
   * membership question affirmatively; `false` leaves `session.state`
   * carrying the failure until the card retries or dismisses it.
   */
  ensure(symbol: string, mode: BackfillableMode): CoverageGateSession {
    if (this.isHeld(symbol, mode)) {
      return CoverageGateSessionImpl.alreadyHeld();
    }
    // Coalesce only on the full gate identity: coverage is per tree, so a
    // `raw` gate says nothing about whether the same symbol is runnable in
    // the split-adjusted tree another picker is reading.
    const entry = this.current;
    if (
      entry !== null &&
      !entry.settled &&
      entry.symbol === symbol &&
      entry.mode === mode
    ) {
      return this.attach(entry);
    }
    this.abandonActive();

    const next: ActiveRun = {
      token: this.nextToken++,
      symbol,
      mode,
      sessions: new Set(),
      finished: null,
      jobRun: null,
      jobId: null,
      settled: false,
    };
    this.activeState.set({
      symbol,
      mode,
      phase: 'backfilling',
      percent: null,
      reason: null,
      message: null,
    });
    this.current = next;
    next.finished = this.runBackfill(symbol, mode, next);
    void next.finished.then(() => {
      // A settled run — covered, failed, cancelled or superseded — stops
      // coalescing, so the same symbol is freely re-ensurable while the
      // failed strip it left behind stays on screen until retry or
      // dismissal.
      next.settled = true;
    });
    return this.attach(next);
  }

  /**
   * Record a refusal the gate cannot act on — an unbackfillable adjustment
   * view, say. Returns the same strip session a run would, so the card has
   * one rendering path for "this pick cannot be covered". A refusal touches
   * no running gate: it is this session's verdict alone.
   */
  refuse(
    symbol: string,
    mode: PriceAdjustmentMode,
    reason: string,
    message: string,
  ): CoverageGateSession {
    return CoverageGateSessionImpl.refused({
      symbol,
      mode,
      phase: 'failed',
      percent: null,
      reason,
      message,
    });
  }

  /**
   * Release one session. When it is the last session attached to the live
   * run, the run is torn down and its job cancelled; a superseded or
   * already-settled release is a local no-op — never another card's gate.
   */
  private async releaseSession(session: CoverageGateSessionImpl): Promise<void> {
    session.detach();
    const entry = this.current;
    if (entry === null || !entry.sessions.delete(session)) return;
    if (entry.sessions.size > 0 || this.current !== entry) return;
    this.current = null;
    this.activeState.set(null);
    entry.settled = true;
    entry.jobRun?.detach();
    // The operator cancelled before completion: stop the job once its id is
    // known. A refusal to cancel propagates; a job that survives simply
    // finishes writing in the background.
    if (entry.jobId !== null) await this.jobs.cancelJob(entry.jobId);
  }

  /**
   * A handle on `entry`'s run. The strip is the service's active gate state
   * while this session is attached, with the percent lifted live off the
   * shared runner's progress signal; `done` resolves with the run's verdict
   * unless this session detaches first. The release closure captures the
   * session itself, so cancelling releases exactly this session — the
   * service decides whether that stops the run, and no caller ever has to
   * recognize "its" gate by the state's fields.
   */
  private attach(entry: ActiveRun): CoverageGateSession {
    const detachedFlag = signal(false);
    let resolveDone: ((ready: boolean) => void) | null = null;
    const done = new Promise<boolean>((resolve) => {
      resolveDone = resolve;
      void entry.finished?.then((ready) => resolve(detachedFlag() ? false : ready));
    });
    const state = computed(() => {
      if (detachedFlag()) return null;
      const base = this.activeState();
      if (base === null || base.symbol !== entry.symbol || base.mode !== entry.mode) {
        return null;
      }
      const tick = entry.jobRun?.progress() ?? null;
      const percent =
        tick !== null && tick.total > 0
          ? Math.min(100, Math.round((tick.current / tick.total) * 100))
          : null;
      return { ...base, percent };
    });
    const session: CoverageGateSessionImpl = new CoverageGateSessionImpl(
      () => this.releaseSession(session),
      state,
      done,
      () => {
        if (!detachedFlag()) {
          detachedFlag.set(true);
          resolveDone?.(false);
        }
      },
    );
    entry.sessions.add(session);
    return session;
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
    entry.settled = true;
    entry.jobRun?.detach();
    for (const session of entry.sessions) session.detach();
    entry.sessions.clear();
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

    let jobRun: BackfillJobRun;
    try {
      jobRun = await this.runner.start(spec);
    } catch (error) {
      if (!(error instanceof BackfillSubmissionError)) throw error;
      return this.fail(entry, 'backfill_submission_failed', error.message);
    }
    // The operator cancelled or moved on while submission was in flight:
    // the job id only exists now, so stop the just-accepted job here.
    if (!this.live(entry.token)) {
      await jobRun.cancel();
      return false;
    }
    entry.jobRun = jobRun;
    entry.jobId = jobRun.jobId;

    const terminal = await jobRun.terminal;
    if (!this.live(entry.token)) return false;
    switch (terminal.kind) {
      case 'failed':
        return this.fail(entry, terminal.code, terminal.message);
      case 'cancelled':
        return this.fail(
          entry,
          'backfill_cancelled',
          'The backfill was cancelled.',
        );
      case 'detached':
        return false;
      case 'completed':
        return await this.settle(symbol, mode, entry);
    }
  }

  /**
   * A completed job is not a covered symbol until the lake says so — and
   * the ask is a **fresh** read, not the cached catalog: `reload()` starts
   * an asynchronous load while the pool still holds the pre-backfill
   * answer, so judging membership against it would report `backfill_empty`
   * for bars that just landed. The reload still runs, to refresh every
   * picker's pool; the verdict comes from the read this gate awaits.
   */
  private async settle(
    symbol: string,
    mode: BackfillableMode,
    entry: ActiveRun,
  ): Promise<boolean> {
    this.lake.viewFor(mode).reload();
    const read = await this.dataLake.storageSummary('usa', mode, 'trade');
    if (!this.live(entry.token)) return false;
    if (read.kind !== 'ok') {
      return this.fail(entry, 'coverage_unknown', read.message);
    }
    if (read.value.symbols.some((span) => span.symbol === symbol && isRunnableSpan(span))) {
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
        mode: entry.mode,
        phase: 'failed',
        percent: null,
        reason,
        message,
      });
    }
    return false;
  }
}

/**
 * The one in-flight gate's bookkeeping. Every late asynchronous
 * continuation carries the run's token and re-checks `live(token)` before
 * it may touch state, so a teardown landing mid-submission or mid-await
 * never resurrects a dismissed strip or orphans a just-accepted job.
 */
interface ActiveRun {
  readonly token: number;
  readonly symbol: string;
  /** The lake tree this run fills — part of the gate's identity. */
  readonly mode: BackfillableMode;
  /** The cards currently rendering (or awaiting) this run's strip. */
  readonly sessions: Set<CoverageGateSessionImpl>;
  /** The run's overall verdict — assigned immediately after `runBackfill`. */
  finished: Promise<boolean> | null;
  /** The runner's handle once the server accepted the job. */
  jobRun: BackfillJobRun | null;
  /** The job's id once accepted — `null` while submitting. */
  jobId: string | null;
  settled: boolean;
}

class CoverageGateSessionImpl implements CoverageGateSession {
  constructor(
    private readonly release: () => Promise<void>,
    readonly state: Signal<CoverageGateState | null>,
    readonly done: Promise<boolean>,
    private readonly detachFn: () => void,
  ) {}

  /** A gate that is already satisfied — the lake holds the symbol. */
  static alreadyHeld(): CoverageGateSessionImpl {
    return new CoverageGateSessionImpl(
      async () => {},
      signal<CoverageGateState | null>(null).asReadonly(),
      Promise.resolve(true),
      () => {},
    );
  }

  /** A refusal the gate cannot act on — rendered like any failure. */
  static refused(state: CoverageGateState): CoverageGateSessionImpl {
    return new CoverageGateSessionImpl(
      async () => {},
      signal<CoverageGateState | null>(state).asReadonly(),
      Promise.resolve(false),
      () => {},
    );
  }

  /** Detach: the strip leaves the screen and this waiter resolves `false`. */
  detach(): void {
    this.detachFn();
  }

  cancel(): Promise<void> {
    return this.release();
  }
}
