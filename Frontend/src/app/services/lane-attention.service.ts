import { HttpClient } from '@angular/common/http';
import { DestroyRef, Injectable, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { components } from '../api/broker.types';

const POLL_INTERVAL_MS = 5000;

/**
 * Ceiling for the backed-off poll cadence, doubling from `POLL_INTERVAL_MS`
 * while the aggregate read itself keeps failing. Same shape and 10x ratio as
 * `AlpacaLiveVerdictService`'s backoff (500 ms base, 5 s cap there; 5 s base,
 * 50 s cap here) — one dead coordinator stretches the cadence to at most
 * 50 s instead of hammering it every 5 s forever.
 */
const POLL_BACKOFF_MAX_MS = 50_000;

const AGGREGATE_ATTENTION_URL = '/api/broker-clerks/aggregate/attention';

/** One condition currently needing the operator on a lane — the committed
 * contract's own shape (`GET /api/brokers/{broker}/attention`). */
export type LaneAttentionItem = components['schemas']['LaneAttentionItem'];

/** The coordinator's per-lane fold of one aggregate poll. Declared here, not
 * in the OpenAPI contract, because the aggregate route returns the fleet
 * router's provenance-preserving partial-aggregation envelope verbatim — the
 * same decision `FleetDirectoryService` makes for `/api/broker-clerks`. */
export interface AggregateAttentionLane {
  readonly broker: string;
  readonly clerk_id: string;
  readonly ok: boolean;
  readonly value?: components['schemas']['LaneAttentionRead'];
  readonly error_reason?: string;
  readonly error_message?: string;
}

/** One aggregate poll's whole body. */
export interface AggregateAttentionResponse {
  readonly observed_at_ms: number;
  readonly lanes: readonly AggregateAttentionLane[];
}

/** One lane's latest attention read. `unknown` is the coordinator's own
 * judgment that the lane's read could not be completed (`ok: false`) — the
 * bell renders it grey and never as "quiet", the same fail-closed stance the
 * backend fold takes (#2228). `errorReason` carries that fold's refusal
 * reason (a backend identifier, rendered through `receiptLabel`). */
export interface LaneAttentionState {
  readonly unknown: boolean;
  readonly items: readonly LaneAttentionItem[];
  readonly errorReason: string | null;
}

/** What a lane reads as before its first poll response lands: quiet, not
 * unknown — an unread lane has no conditions to show and no failed read to
 * warn about. */
export const UNPOLLED_LANE_ATTENTION_STATE: LaneAttentionState = Object.freeze({
  unknown: false,
  items: [],
  errorReason: null,
});

/** Quiet and unknown are different facts: quiet is `ok: true` with no items. */
export const QUIET_LANE_ATTENTION_STATE: LaneAttentionState = Object.freeze({
  unknown: false,
  items: [],
  errorReason: null,
});

/**
 * Singleton owner of the per-lane attention signal, keyed by `clerk_id`
 * (#2228). One poll per tick — `GET /api/broker-clerks/aggregate/attention` —
 * because the coordinator already fans the read out to every lane
 * server-side; the shell renders one bell per lane from its own slice of the
 * fold and never merges slices.
 *
 * Isolation is the server's contract, restated here: one lane's `ok: false`
 * only ever touches that lane's entry, and the coordinator combines no
 * values, so each `clerk_id`'s items are that lane's clerk's own answer,
 * untouched.
 *
 * A failed aggregate request keeps every lane's last known items rather than
 * blanking them: unlike a mode verdict (which must never render stale over a
 * network fault), a stale attention list errs in the safe direction — an
 * episode that resolved during the outage keeps ringing until the next
 * successful read, and no new episode can be silently missed by holding old
 * state. `unknown` itself is only ever written from the server's per-lane
 * fold, never inferred from transport failure here.
 *
 * The cadence backs off — doubling, capped at `POLL_BACKOFF_MAX_MS` — while
 * the aggregate read keeps failing, and resets to `POLL_INTERVAL_MS` on the
 * first success. No directory dependency: the aggregate response is itself
 * the current roster of attention-serving lanes, pruned on every success.
 */
@Injectable({ providedIn: 'root' })
export class LaneAttentionService {
  private readonly http = inject(HttpClient);
  private readonly destroyRef = inject(DestroyRef);

  private readonly _stateByClerkId = signal<ReadonlyMap<string, LaneAttentionState>>(new Map());
  readonly stateByClerkId = this._stateByClerkId.asReadonly();

  private pollTimer: ReturnType<typeof setTimeout> | null = null;
  private started = false;

  /** The delay before the next scheduled tick. Doubles (capped) on a failed
   * aggregate read, resets to `POLL_INTERVAL_MS` on a successful one — a
   * self-rescheduling `setTimeout` chain for the same reason the verdict
   * poller uses one. */
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
   * when it settled, so a slow aggregate read never stretches the cadence. */
  private runTick(): void {
    const tickStartedAtMs = Date.now();
    void this.refresh().finally(() => this.scheduleNextTick(tickStartedAtMs));
  }

  /** Arm the next tick at the current (possibly backed-off) interval, minus
   * however much of it this tick already spent settling. A no-op once
   * `stop()` has run, so a tick in flight at teardown can't resurrect the
   * timer. */
  private scheduleNextTick(tickStartedAtMs: number): void {
    if (!this.started) return;
    const elapsedMs = Date.now() - tickStartedAtMs;
    const delayMs = Math.max(0, this.currentIntervalMs - elapsedMs);
    this.pollTimer = setTimeout(() => this.runTick(), delayMs);
  }

  /** This lane's latest state, or the unpolled default if it has never been
   * reported by a fold. */
  stateFor(clerkId: string): LaneAttentionState {
    return this._stateByClerkId().get(clerkId) ?? UNPOLLED_LANE_ATTENTION_STATE;
  }

  /** One polling tick: the single aggregate read, folded per lane. */
  async refresh(): Promise<void> {
    let response: AggregateAttentionResponse;
    try {
      response = await firstValueFrom(
        this.http.get<AggregateAttentionResponse>(AGGREGATE_ATTENTION_URL),
      );
    } catch {
      // Handled where it belongs, not swallowed: the failure folds into the
      // cadence backoff below, and every lane keeps its last known state
      // (see the class docstring for why that is the safe direction here).
      this.applyBackoff(true);
      return;
    }
    this.storeLanes(response.lanes);
    this.applyBackoff(false);
  }

  /** Store each lane's slice of the fold and prune lanes the roster no
   * longer reports. `ok: false` becomes that lane's explicit unknown — never
   * a substitution of another lane's answer and never an omission. */
  private storeLanes(lanes: readonly AggregateAttentionLane[]): void {
    const next = new Map(this._stateByClerkId());
    const seen = new Set<string>();
    for (const lane of lanes) {
      seen.add(lane.clerk_id);
      next.set(
        lane.clerk_id,
        lane.ok && lane.value
          ? {
              unknown: false,
              items: dedupeByConditionId(lane.value.items),
              errorReason: null,
            }
          : { unknown: true, items: [], errorReason: lane.error_reason ?? null },
      );
    }
    for (const clerkId of [...next.keys()]) {
      if (!seen.has(clerkId)) next.delete(clerkId);
    }
    this._stateByClerkId.set(next);
  }

  /** Doubling-capped backoff for the poll cadence — governs only the
   * `setTimeout` gap between ticks, not per-request retries. */
  private applyBackoff(failed: boolean): void {
    this.currentIntervalMs = failed
      ? Math.min(this.currentIntervalMs * 2, POLL_BACKOFF_MAX_MS)
      : POLL_INTERVAL_MS;
  }

  private stop(): void {
    if (this.pollTimer !== null) {
      clearTimeout(this.pollTimer);
      this.pollTimer = null;
    }
    this.started = false;
  }
}

/** The stable identity the bell dedupes by: one condition id, one item, even
 * if a server-side fold ever repeated a row. An item disappears exactly when
 * its episode resolves, so first-wins keeps the oldest observation's copy —
 * the one the operator has been looking at. */
function dedupeByConditionId(
  items: readonly LaneAttentionItem[],
): readonly LaneAttentionItem[] {
  const byId = new Map<string, LaneAttentionItem>();
  for (const item of items) {
    if (!byId.has(item.condition_id)) byId.set(item.condition_id, item);
  }
  return [...byId.values()];
}
