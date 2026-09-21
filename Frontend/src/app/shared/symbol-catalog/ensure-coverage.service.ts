import {
  Injectable,
  computed,
  inject,
  signal,
  type Signal,
  type WritableSignal,
} from '@angular/core';

import { DataLakeService } from '../data-lake';
import {
  BackfillJobRunner,
  BackfillSubmissionError,
  type BackfillJobRun,
} from '../data-lake/backfill-job-runner';
import {
  rootFailureOf,
  toBackfillDayEvent,
  tradingDateToMs,
  tradingRangeRejection,
  tradingRangeSpanDays,
} from '../data-lake';
import type { BackfillFailure, DataRunSpec, PriceAdjustmentMode } from '../data-lake';
import { isRunnableSpan } from '../ticker-catalog';
import { TickerCatalogService } from '../ticker-catalog';
import { etIsoDate, isoDateAfter } from '../date/et-midnight';
import { toMostRecentTradingDayIso, toNextTradingDayIso } from '../date/weekday';

/**
 * The widest backfill window that clears **both** bounds on a backfill:
 * the data plane's inclusive range cap, and the provider's own history
 * entitlement. It ends at the most recent trading day on or before
 * `todayIso`.
 *
 * Two different things can refuse this window, and satisfying one is not
 * satisfying the other:
 *
 * - *The cap* counts inclusively (`(end - start).days + 1`), and the weekday
 *   walk-back on the endpoints can add up to three more days (1830 is not a
 *   whole number of weeks), which is how a window composed on a Monday
 *   landed on a 422 while the same code passed on a Saturday. The start
 *   therefore begins four days inside the cap and steps forward — a step
 *   that keeps both endpoints on weekdays — until the inclusive span fits.
 * - *The entitlement* is the oldest day the provider will serve, which the
 *   data plane reports on `/backfill-defaults`. `capDays` is emphatically
 *   not a proxy for it: it is a validation ceiling padded to `5 * 366` for
 *   leap years, so `cap - 4` is exactly five years, and five years to the
 *   day is the one day Polygon's five-year plan excludes. That put every
 *   unheld pick's first day outside the plan, and because
 *   `provider_entitlement_error` is globally fatal in the backfill worker,
 *   one such day aborted the whole run before a single bar was written
 *   (#2241). The floor is therefore clamped, and snapped forward off a
 *   weekend — walking it back is what would step outside it again.
 *
 * The caller still runs the result through `tradingRangeRejection`, so a cap
 * the data plane lowered, or a floor that has overtaken the end date, fails
 * loudly instead of shipping a window that cannot be served.
 */
export function fitBackfillWindow(
  todayIso: string,
  capDays: number,
  historyStartIso: string,
): { start: string; end: string } {
  const end = toMostRecentTradingDayIso(todayIso);
  const capStart = toMostRecentTradingDayIso(end, -(capDays - 4));
  // Both are zero-padded `YYYY-MM-DD`, so lexicographic order is chronological.
  let start = toNextTradingDayIso(capStart < historyStartIso ? historyStartIso : capStart);
  let guard = 0;
  while (guard <= 7 && (tradingRangeSpanDays(start, end) ?? capDays + 1) > capDays) {
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

/**
 * The mode narrowed to what the fetch pipeline can actually write, or `null`
 * for every view whose rows arrive by import. The single card, the multi
 * card and the backfill panel all refuse loudly on `null` instead of
 * submitting a spec that changes nothing.
 */
export function toBackfillableMode(
  mode: PriceAdjustmentMode | null,
): BackfillableMode | null {
  return mode === 'raw' || mode === 'polygon_split_adjusted' ? mode : null;
}

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
 * Concurrent cards own independent sessions. Identical `{symbol, mode}`
 * asks coalesce onto one run; unrelated asks never supersede one another.
 * Submission, the SSE fold and terminal classification are owned by
 * `BackfillJobRunner`; this module adds only the coverage question:
 * completion is verified against a **fresh** lake read before any session
 * may proceed.
 */
@Injectable({ providedIn: 'root' })
export class EnsureCoverageService {
  private readonly lake = inject(TickerCatalogService);
  private readonly dataLake = inject(DataLakeService);
  private readonly runner = inject(BackfillJobRunner);

  private readonly runs = new Map<string, ActiveRun>();

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
    const key = gateKey(symbol, mode);
    const entry = this.runs.get(key);
    if (entry !== undefined && !entry.settled && entry.symbol === symbol && entry.mode === mode) {
      return this.attach(entry);
    }
    const next = new ActiveRun(key, symbol, mode, (run) => this.runBackfill(symbol, mode, run));
    this.runs.set(key, next);
    void next.finished.then(() => this.finishRun(next));
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
  private async releaseSession(entry: ActiveRun, session: CoverageGateSessionImpl): Promise<void> {
    session.detach();
    if (!entry.sessions.delete(session)) return;
    if (entry.sessions.size > 0 || entry.settled) return;
    entry.settled = true;
    if (this.runs.get(entry.key) === entry) this.runs.delete(entry.key);
    entry.jobRun?.detach();
    // The operator cancelled before completion: stop the job once its id is
    // known. A refusal to cancel propagates; a job that survives simply
    // finishes writing in the background.
    if (entry.jobRun !== null) await entry.jobRun.cancel();
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
      void entry.finished.then((ready) => resolve(detachedFlag() ? false : ready));
    });
    const state = computed(() => {
      if (detachedFlag()) return null;
      const base = entry.state();
      if (base === null) return null;
      const lifecycle = entry.jobRun?.state() ?? null;
      const tick = lifecycle?.kind === 'running' ? lifecycle.progress : null;
      const percent =
        tick !== null && tick.total > 0
          ? Math.min(100, Math.round((tick.current / tick.total) * 100))
          : null;
      return { ...base, percent };
    });
    const session: CoverageGateSessionImpl = new CoverageGateSessionImpl(
      () => this.releaseSession(entry, session),
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

  private finishRun(entry: ActiveRun): void {
    entry.settled = true;
    if (this.runs.get(entry.key) === entry) this.runs.delete(entry.key);
  }

  private live(entry: ActiveRun): boolean {
    return !entry.settled && this.runs.get(entry.key) === entry;
  }

  private async runBackfill(
    symbol: string,
    mode: BackfillableMode,
    entry: ActiveRun,
  ): Promise<boolean> {
    const defaults = await this.dataLake.backfillDefaults();
    if (!this.live(entry)) return false;
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
    const { start, end } = fitBackfillWindow(
      todayIso,
      defaults.value.max_trading_range_days,
      etIsoDate(defaults.value.provider_history_start_ms),
    );
    const rejection = tradingRangeRejection(start, end, defaults.value.max_trading_range_days);
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
      // The per-day frames are the only place the run says *why* it wrote
      // nothing; without them a run the provider refused outright is
      // indistinguishable from a symbol that genuinely has no bars.
      jobRun = await this.runner.start(spec, {
        onDomainEvent: (event) => entry.observeFailures(toBackfillDayEvent(event)?.failures),
      });
    } catch (error) {
      if (!(error instanceof BackfillSubmissionError)) throw error;
      return this.fail(entry, 'backfill_submission_failed', error.message);
    }
    // The operator cancelled while submission was in flight: the job handle
    // only exists now, so stop the just-accepted job here.
    if (!this.live(entry)) {
      await jobRun.cancel();
      return false;
    }
    entry.jobRun = jobRun;

    const terminal = await jobRun.terminal;
    if (!this.live(entry)) return false;
    switch (terminal.kind) {
      case 'failed':
        return this.fail(entry, terminal.code, terminal.message);
      case 'cancelled':
        return this.fail(entry, 'backfill_cancelled', 'The backfill was cancelled.');
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
   *
   * The lake stays the arbiter of "did this pick become runnable" — a run
   * that failed some days but landed others is a good pick — so the run's
   * own failures are consulted only once the lake has said no, and then to
   * name the reason rather than to reach the verdict.
   */
  private async settle(symbol: string, mode: BackfillableMode, entry: ActiveRun): Promise<boolean> {
    this.lake.viewFor(mode).reload();
    const read = await this.dataLake.storageSummary('usa', mode, 'trade');
    if (!this.live(entry)) return false;
    if (read.kind !== 'ok') {
      return this.fail(entry, 'coverage_unknown', read.message);
    }
    if (read.value.symbols.some((span) => span.symbol === symbol && isRunnableSpan(span))) {
      entry.state.set(null);
      return true;
    }
    // The job framework reports a run the provider refused as `job.completed`
    // — the *job* ran; the *backfill* did not — so a gate that only watched
    // the lifecycle could say nothing beyond "empty". The typed reason was
    // on the wire all along (#2241).
    const failure = entry.rootFailure;
    if (failure !== null) {
      return this.fail(
        entry,
        failure.reason,
        failure.detail ??
          'The backfill wrote no bars for this symbol and reported no detail.',
      );
    }
    return this.fail(
      entry,
      'backfill_empty',
      'The backfill finished, but the lake still holds no bars for this symbol.',
    );
  }

  private fail(entry: ActiveRun, reason: string, message: string): false {
    if (this.live(entry)) {
      entry.state.set({
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
 * One keyed gate's bookkeeping. Every late asynchronous continuation
 * re-checks that this exact run still owns its map entry before touching
 * state, so teardown cannot resurrect a dismissed strip or orphan a newly
 * accepted job.
 */
function gateKey(symbol: string, mode: BackfillableMode): string {
  return `${mode}\u0000${symbol}`;
}

class ActiveRun {
  readonly sessions = new Set<CoverageGateSessionImpl>();
  readonly state: WritableSignal<CoverageGateState | null>;
  readonly finished: Promise<boolean>;
  jobRun: BackfillJobRun | null = null;
  settled = false;
  /**
   * The failure that explains an empty run, kept from the first day that
   * reported one. The worker walks oldest day first and stops on a globally
   * fatal reason, so a later day cannot be a better explanation than the
   * one that ended the run.
   */
  rootFailure: BackfillFailure | null = null;

  /** Fold one day's failures; the first real cause wins and is kept. */
  observeFailures(failures: readonly BackfillFailure[] | undefined): void {
    if (this.rootFailure !== null || failures === undefined) return;
    this.rootFailure = rootFailureOf(failures);
  }

  constructor(
    readonly key: string,
    readonly symbol: string,
    readonly mode: BackfillableMode,
    start: (run: ActiveRun) => Promise<boolean>,
  ) {
    this.state = signal({
      symbol,
      mode,
      phase: 'backfilling',
      percent: null,
      reason: null,
      message: null,
    });
    this.finished = start(this);
  }
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
    const stateSignal = signal<CoverageGateState | null>(state);
    return new CoverageGateSessionImpl(
      async () => stateSignal.set(null),
      stateSignal.asReadonly(),
      Promise.resolve(false),
      () => stateSignal.set(null),
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
