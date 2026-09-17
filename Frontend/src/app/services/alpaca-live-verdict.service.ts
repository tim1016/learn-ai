import { DestroyRef, Injectable, inject, signal } from '@angular/core';

import type { AlpacaLiveVerdict } from '../api/alpaca.types';
import { FleetDirectoryService } from '../fleet/fleet-directory.service';
import type { LaneDescriptor } from '../fleet/fleet-directory.types';
import { resourceTarget } from '../fleet/resource-target';
import { BrokersService } from './brokers.service';

const POLL_INTERVAL_MS = 5000;

/**
 * Ceiling for the backed-off poll cadence, doubling from `POLL_INTERVAL_MS`
 * on sustained fleet-wide failure (see `applyBackoff`). Same 10x ratio as
 * the reconnect backoff this is adapted from (`authenticated-sse-connection.ts`:
 * 500 ms base, 5 s cap) — a fleet-wide outage stretches the cadence to at
 * most 50 s instead of hammering every lane every 5 s forever.
 */
const POLL_BACKOFF_MAX_MS = 50_000;

/**
 * How many `roster()` ticks separate one forced `directory.refresh()` from
 * the next. `FleetDirectoryService.ensureLoaded()` is a permanent no-op once
 * one load has succeeded, so left alone this trust anchor would never learn
 * about a lane provisioned after the tab's first successful directory load.
 * At the `POLL_INTERVAL_MS` (5 s) cadence, 6 ticks is one forced refresh
 * every 6 * 5 s = 30 s. Under backoff, ticks are simply spaced further
 * apart — the 6-tick count itself is untouched, so a forced refresh still
 * naturally lands less often while the fleet is unhealthy.
 */
const DIRECTORY_REFRESH_EVERY_N_TICKS = 6;

/** One lane's latest live-verdict read, or the error from its last attempt. */
export interface LaneVerdictState {
  readonly verdict: AlpacaLiveVerdict | null;
  readonly lastError: unknown;
}

/** What a lane reads as before its first poll response ever lands. */
export const UNPOLLED_LANE_STATE: LaneVerdictState = Object.freeze({
  verdict: null,
  lastError: null,
});

/** What one `roster()` call produced: the lanes this tick should read, and
 * whether the directory load itself failed — returned together, rather than
 * through a side-channel field, so `refresh()` reads a snapshot instead of
 * racing a future concurrent tick over mutable instance state. */
interface RosterResult {
  readonly lanes: readonly LaneDescriptor[];
  readonly directoryLoadFailed: boolean;
}

/** What one lane's mode chip renders: the tone vocabulary is shared with the
 * shell's live-verdict pills (ADR 0059 D8), so a lane reads the same colour
 * everywhere it is named. */
export interface LaneModeChip {
  readonly tone: 'paper' | 'live' | 'undetermined';
  readonly mode: string;
  /** How many sealed instances are armed for real-money submission, or
   * `null` when the verdict is not a live one — there is no armed count to
   * show for a paper or undetermined lane, which is not the same fact as
   * "zero are armed". */
  readonly armedCount: number | null;
  /** True when the clerk holds the no-submit Shadow authority (ADR 0059 D2):
   * the real live read ports bound to a port that synthesizes fills and
   * submits nothing. Carried beside `mode` rather than replacing it, because
   * a shadowing lane is still reading the live account. */
  readonly shadow: boolean;
}

/** Project one lane's verdict state into its compact chip form.
 *
 * A verdict that is unread, failed, or `unknown` renders the loud
 * undetermined treatment and *says the real-money assumption in its own
 * text* (decision D2, #2110/#2139 — the same fail-closed stance as the
 * pills; grey or neutral "not configured" wording is banned). The verdict
 * stays the only truth source; the chip never guesses a mode from
 * account-id shape or env (ADR 0011 §7), and `shadow` is read from the
 * server's own `clerk_authority` rather than inferred from the account. */
export function verdictModeChip(state: LaneVerdictState): LaneModeChip {
  const verdict = state.verdict;
  const shadow = verdict?.clerk_authority === 'shadow';
  switch (verdict?.final_verdict) {
    case 'paper':
      return { tone: 'paper', mode: 'Paper money', armedCount: null, shadow };
    case 'live-unarmed':
    case 'live-armed':
      return { tone: 'live', mode: 'Live', armedCount: verdict.armed_instance_count, shadow };
    default:
      return {
        tone: 'undetermined',
        mode: 'Mode unknown — assume real money',
        armedCount: null,
        shadow,
      };
  }
}

/**
 * Singleton owner of the Alpaca live-verdict signal, keyed by `clerk_id`
 * (ADR 0059 D8; #2110).
 *
 * Polls `GET /api/brokers/alpaca/clerks/{clerk_id}/live-verdict` for every
 * lane `FleetDirectoryService.lanesOf('alpaca')` currently reports, every
 * five seconds, and exposes the latest per-lane state as one signal. The
 * shell renders one badge per lane from it.
 *
 * FR-093 — one lane's failed **or slow** read must never blank, delay, or
 * overwrite another lane's state — is satisfied at both layers, and it takes
 * both:
 *
 * - **State.** Each lane is stored under its own key, so a rejected read for
 *   one `clerk_id` only ever overwrites that `clerk_id`'s entry.
 * - **Transport.** The tick's reads are handed to the scheduler as one
 *   concurrent group (`BrokersService.getLiveVerdicts`). Per-lane `try/catch`
 *   alone is not enough: the shared scheduler dispatches one read at a time
 *   and spends `POLL_REQUEST_TIMEOUT_MS` from enqueue, so a live lane hanging
 *   15 s used to reject the paper lane's queued read with "the poll ceiling
 *   elapsed while it waited for a turn" — a genuinely live-armed lane losing
 *   its account id and armed count because a sibling was slow.
 *
 * A single poll timer drives every lane's read and the group is one entry in
 * the shared queue, so this is still one poller fanning out to N reads, not
 * N pollers. Single-flight by URL is preserved: a tick that overlaps a still
 * outstanding read for the same lane joins it rather than racing it, so the
 * 5 s timer outliving a 15 s read carries no stale-overwrite hazard.
 *
 * Never derive the mode from an env var or an account-id shape here: the
 * server's verdict is the only source of truth (ADR 0011 §7).
 *
 * The poll cadence backs off — doubling, capped at `POLL_BACKOFF_MAX_MS` —
 * when a tick's failure is widespread (every lane's read failed, or the
 * directory itself failed to load), and resets to `POLL_INTERVAL_MS` the
 * moment a tick is healthy again. A single lane's isolated failure never
 * triggers backoff: that would let one bad lane slow every other lane's
 * badge, which is exactly the cross-lane leakage FR-093 forbids for state.
 * See `applyBackoff`.
 */
@Injectable({ providedIn: 'root' })
export class AlpacaLiveVerdictService {
  private readonly brokers = inject(BrokersService);
  private readonly directory = inject(FleetDirectoryService);
  private readonly destroyRef = inject(DestroyRef);

  private readonly _stateByClerkId = signal<ReadonlyMap<string, LaneVerdictState>>(new Map());
  readonly stateByClerkId = this._stateByClerkId.asReadonly();

  private pollTimer: ReturnType<typeof setTimeout> | null = null;
  private started = false;

  /** Ticks since this service started; drives the every-6th-tick forced
   * directory refresh in `roster()`. */
  private tickCount = 0;

  /** The delay before the next scheduled tick. Doubles (capped) on
   * widespread failure, resets to `POLL_INTERVAL_MS` on a healthy tick. A
   * plain `setInterval` can't express this — its period is fixed at
   * creation — so polling is a self-rescheduling `setTimeout` chain instead. */
  private currentIntervalMs = POLL_INTERVAL_MS;

  constructor() {
    this.destroyRef.onDestroy(() => this.stop());
  }

  /** Begin background polling. Idempotent — the shell calls this once on boot. */
  start(): void {
    if (this.started) return;
    this.started = true;
    this.runTick();
  }

  /** Run one tick and arm the next relative to when THIS tick started, not
   * when it settled. A slow lane's read (up to `POLL_REQUEST_TIMEOUT_MS`)
   * would otherwise tack a full `currentIntervalMs` on top of its own delay
   * before the next tick fires — stalling every other lane's cadence behind
   * the slowest read, which is exactly the cross-lane leakage FR-093
   * forbids. */
  private runTick(): void {
    const tickStartedAtMs = Date.now();
    void this.refresh().finally(() => this.scheduleNextTick(tickStartedAtMs));
  }

  /** Arm the next tick at the current (possibly backed-off) interval, minus
   * however much of it this tick already spent settling. A no-op once
   * `stop()` has run, so a tick already in flight when the service is
   * destroyed can't resurrect the timer after teardown. */
  private scheduleNextTick(tickStartedAtMs: number): void {
    if (!this.started) return;
    const elapsedMs = Date.now() - tickStartedAtMs;
    const delayMs = Math.max(0, this.currentIntervalMs - elapsedMs);
    this.pollTimer = setTimeout(() => this.runTick(), delayMs);
  }

  /** This lane's latest state, or the unpolled default if it has never
   * reported (not yet in the directory, or polled before this lane existed). */
  stateFor(clerkId: string): LaneVerdictState {
    return this._stateByClerkId().get(clerkId) ?? UNPOLLED_LANE_STATE;
  }

  /** One polling tick across every known Alpaca lane. Each lane's read is
   * independent end to end — its own concurrent request, its own try/catch,
   * its own map entry — so one lane's rejection or latency can never touch
   * another's state. */
  async refresh(): Promise<void> {
    const { lanes, directoryLoadFailed } = await this.roster();
    const reads = this.brokers.getLiveVerdicts(
      lanes.map((lane) => resourceTarget(lane.broker, lane.clerk_id)),
    );
    // `getLiveVerdicts` returns synchronously and `storeLane` attaches its
    // handler before its first await, so every read is claimed in this same
    // synchronous block — no read is ever left momentarily unhandled.
    const outcomes = await Promise.all(
      lanes.map((lane, index) => this.storeLane(lane.clerk_id, reads[index])),
    );
    this.pruneTo(new Set(lanes.map((lane) => lane.clerk_id)));
    this.applyBackoff(this.isWidespreadFailure(directoryLoadFailed, lanes.length, outcomes));
  }

  /**
   * Widespread means "the fleet is unhealthy", not "a lane is unhealthy":
   * the directory read itself failed, or — with at least one lane known —
   * every one of this tick's lane reads failed. One lane failing among
   * healthy siblings is never widespread; that's the same FR-093 boundary
   * `storeLane`'s per-key state already enforces, applied to cadence.
   */
  private isWidespreadFailure(
    directoryLoadFailed: boolean,
    laneCount: number,
    outcomes: readonly boolean[],
  ): boolean {
    if (directoryLoadFailed) return true;
    return laneCount > 0 && outcomes.every((succeeded) => !succeeded);
  }

  /**
   * Doubling-capped backoff for the poll cadence, adapted from the
   * reconnect backoff in `authenticated-sse-connection.ts`. This governs
   * only the `setTimeout` gap between ticks — not per-request retries,
   * which stay `PolledReadScheduler`'s job via `POLL_REQUEST_TIMEOUT_MS`.
   */
  private applyBackoff(widespreadFailure: boolean): void {
    this.currentIntervalMs = widespreadFailure
      ? Math.min(this.currentIntervalMs * 2, POLL_BACKOFF_MAX_MS)
      : POLL_INTERVAL_MS;
  }

  /**
   * The lanes this tick reads, driving the directory rather than waiting for
   * some other surface to.
   *
   * Only three broker feature pages and the redirect guard used to call
   * `ensureLoaded`. On every other route a failed directory load left
   * `lanesOf` empty for the whole session, and the shell's trust anchor — the
   * global answer to "is real money at risk?" — stayed blank while a live
   * lane traded. Driving it from the shell's own tick makes a failed load
   * retried, at the directory's own paced cooldown, instead of terminal.
   *
   * `ensureLoaded()` is a permanent no-op once one load has succeeded, so a
   * lane provisioned after the tab's first successful load would never earn
   * a badge. Every `DIRECTORY_REFRESH_EVERY_N_TICKS`th tick forces a real
   * `directory.refresh()` instead, so the roster this trust anchor votes from
   * is never frozen at page load for longer than that window. The staleness
   * tolerance lives here, not inside `ensureLoaded`, because `ensureLoaded`'s
   * other callers are redirect guards that need lanes resolved before
   * deciding where a URL lands — a max-age there would occasionally block
   * navigation on a network request.
   */
  private async roster(): Promise<RosterResult> {
    this.tickCount += 1;
    const dueForRefresh = this.tickCount % DIRECTORY_REFRESH_EVERY_N_TICKS === 0;
    let directoryLoadFailed: boolean;
    try {
      // `refresh()` rejects where `ensureLoaded()` may resolve; this catch
      // covers both.
      if (dueForRefresh) {
        await this.directory.refresh();
      } else {
        await this.directory.ensureLoaded();
      }
      directoryLoadFailed = false;
    } catch {
      // Handled where it belongs, not swallowed: FleetDirectoryService owns
      // this error and already exposes it as observable state. An unresolved
      // roster is exactly the condition the shell's "lanes unknown" badge
      // renders, so this tick reads whatever the directory last knew and the
      // next tick asks again. `refresh()` also folds this into the
      // widespread-failure check that drives cadence backoff.
      directoryLoadFailed = true;
    }
    return { lanes: this.directory.lanesOf('alpaca'), directoryLoadFailed };
  }

  /** Stores this lane's read outcome and reports whether it succeeded, so
   * `refresh()` can fold it into the widespread-failure check without
   * touching this lane's own FR-093 isolation. */
  private async storeLane(clerkId: string, read: Promise<AlpacaLiveVerdict>): Promise<boolean> {
    try {
      this.setState(clerkId, { verdict: await read, lastError: null });
      return true;
    } catch (err) {
      // A failed read means this lane's verdict is unknown to this client;
      // never keep rendering a stale mode over a network fault.
      this.setState(clerkId, { verdict: null, lastError: err });
      return false;
    }
  }

  private setState(clerkId: string, state: LaneVerdictState): void {
    this._stateByClerkId.update((current) => {
      const next = new Map(current);
      next.set(clerkId, state);
      return next;
    });
  }

  /** Forget lanes the directory no longer reports. The roster is
   * authoritative, and a retained entry would render a pre-departure verdict
   * for a returning lane before its first fresh read lands. */
  private pruneTo(clerkIds: ReadonlySet<string>): void {
    this._stateByClerkId.update((current) => {
      const stale = [...current.keys()].filter((clerkId) => !clerkIds.has(clerkId));
      if (stale.length === 0) return current;
      const next = new Map(current);
      for (const clerkId of stale) next.delete(clerkId);
      return next;
    });
  }

  private stop(): void {
    if (this.pollTimer !== null) {
      clearTimeout(this.pollTimer);
      this.pollTimer = null;
    }
    this.started = false;
  }
}
